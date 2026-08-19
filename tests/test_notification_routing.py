"""Audience resolution for order notifications.

Covers the redesign in ``docs/NOTIFICATIONS_REDESIGN_2026-08-19.md``:
who receives what, and — more importantly — the three-filter invariant
that decides it. Consent is absolute; relevance and reachability yield
rather than leave an event with nobody to send to.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.customer import Customer, CustomerContact
from app.models.enums import CustomerContactRole, OrderStatus, UserRole
from app.models.order import Order, OrderStatusHistory
from app.models.user import User
from app.security.passwords import hash_password
from app.services.notification_prefs import (
    NotificationEvent,
    NotificationPrefs,
    NotificationScope,
    NotificationSide,
    parse_form,
    prefs_for_contact,
    prefs_for_user,
)

pytestmark = pytest.mark.postgres

BASE_URL = "https://4mex.example"


# ---------------------------------------------------------------- helpers


def _prefs(*, side: NotificationSide, role, off=(), scope=None) -> dict:
    """Serialised prefs with ``off`` events disabled and an optional scope."""
    base = NotificationPrefs.defaults(side, role)
    events = frozenset(e for e in base.events if e not in set(off))
    return NotificationPrefs(side=side, events=events, scope=scope or base.scope).to_dict()


async def _seed(
    owner_engine,
    tenant_id: UUID,
    *,
    staff: list[dict] | None = None,
    contacts: list[dict] | None = None,
    order_status: OrderStatus = OrderStatus.DRAFT,
) -> dict:
    """Build a tenant with the requested staff and contacts, plus an order.

    ``staff`` / ``contacts`` entries are kwargs overriding the row
    defaults, so a test can say ``{"role": UserRole.TENANT_STAFF}`` and
    ignore everything else.
    """
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    now = datetime.now(UTC)
    async with sm() as session, session.begin():
        customer = Customer(id=uuid4(), tenant_id=tenant_id, name="ACME", ico="11111111")
        session.add(customer)
        await session.flush()

        users = []
        for i, spec in enumerate([{}] if staff is None else staff):
            users.append(
                User(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    email=spec.pop("email", f"staff{i}@4mex.cz"),
                    full_name=spec.pop("full_name", f"Staff {i}"),
                    role=spec.pop("role", UserRole.TENANT_ADMIN),
                    password_hash=spec.pop("password_hash", hash_password("staffpass")),
                    **spec,
                )
            )

        people = []
        for i, spec in enumerate([{}] if contacts is None else contacts):
            people.append(
                CustomerContact(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    customer_id=customer.id,
                    email=spec.pop("email", f"contact{i}@acme.cz"),
                    full_name=spec.pop("full_name", f"Contact {i}"),
                    role=spec.pop("role", CustomerContactRole.CUSTOMER_USER),
                    password_hash=hash_password("contactpass"),
                    invited_at=now,
                    accepted_at=spec.pop("accepted_at", now),
                    **spec,
                )
            )
        session.add_all([*users, *people])
        await session.flush()

        order = Order(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=customer.id,
            number="2026-000001",
            title="Bracket",
            status=order_status,
        )
        session.add(order)
        await session.flush()
        return {"customer": customer, "staff": users, "contacts": people, "order": order}


def _emails(payloads) -> set[str]:
    return {p.recipient.email for p in payloads}


# ------------------------------------------------------- the original bug


async def test_operator_receives_submitted_orders(owner_engine, demo_tenant, settings) -> None:
    """The whole point of the redesign.

    ``tenant_staff`` is the default role and the pre-selected option in
    the invite form, yet the old ``_staff_recipients`` filtered on
    ``role == TENANT_ADMIN``. The person hired to run production was the
    one person never told an order had arrived.
    """
    from app.services.notification_service import build_order_submitted

    seeded = await _seed(
        owner_engine,
        demo_tenant.id,
        staff=[
            {"email": "admin@4mex.cz", "role": UserRole.TENANT_ADMIN},
            {"email": "operator@4mex.cz", "role": UserRole.TENANT_STAFF},
        ],
    )
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        order = (
            await session.execute(select(Order).where(Order.id == seeded["order"].id))
        ).scalar_one()
        payloads = await build_order_submitted(
            session, tenant=demo_tenant, order=order, base_url=BASE_URL, settings=settings
        )

    assert _emails(payloads) == {"admin@4mex.cz", "operator@4mex.cz"}


async def test_submitting_staff_member_is_not_emailed_their_own_click(
    owner_engine, demo_tenant, settings
) -> None:
    """``build_order_submitted`` used to skip actor exclusion entirely.

    Staff may drive DRAFT -> SUBMITTED themselves (CLAUDE.md §18), so the
    admin who clicked got a mail announcing their own action.
    """
    from app.services.notification_service import build_order_submitted

    seeded = await _seed(
        owner_engine,
        demo_tenant.id,
        staff=[{"email": "admin@4mex.cz"}, {"email": "operator@4mex.cz"}],
    )
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        order = (
            await session.execute(select(Order).where(Order.id == seeded["order"].id))
        ).scalar_one()
        payloads = await build_order_submitted(
            session,
            tenant=demo_tenant,
            order=order,
            base_url=BASE_URL,
            settings=settings,
            actor_email="admin@4mex.cz",
        )

    assert _emails(payloads) == {"operator@4mex.cz"}


# --------------------------------------------------- consent is absolute


async def test_opting_out_is_never_overridden(owner_engine, demo_tenant, settings) -> None:
    """The one filter that must not yield.

    Even when the opt-out empties the audience entirely, nobody is
    re-added — unlike scope and reachability, which do yield.
    """
    from app.services.notification_service import build_order_submitted

    seeded = await _seed(
        owner_engine,
        demo_tenant.id,
        staff=[
            {
                "email": "admin@4mex.cz",
                "notification_prefs": _prefs(
                    side=NotificationSide.STAFF,
                    role=UserRole.TENANT_ADMIN,
                    off=[NotificationEvent.ORDER_SUBMITTED],
                ),
            }
        ],
    )
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        order = (
            await session.execute(select(Order).where(Order.id == seeded["order"].id))
        ).scalar_one()
        payloads = await build_order_submitted(
            session, tenant=demo_tenant, order=order, base_url=BASE_URL, settings=settings
        )

    assert payloads == []


# ------------------------------------------------------- relevance yields


async def test_narrow_scope_wins_when_somebody_else_is_in_scope(
    owner_engine, demo_tenant, settings
) -> None:
    """An operator scoped to their own work is left alone — once the order
    has an owner, and that owner is somebody else."""
    from app.services.notification_service import build_order_submitted

    seeded = await _seed(
        owner_engine,
        demo_tenant.id,
        staff=[
            {"email": "admin@4mex.cz", "role": UserRole.TENANT_ADMIN},
            {
                "email": "operator@4mex.cz",
                "role": UserRole.TENANT_STAFF,
                "notification_prefs": _prefs(
                    side=NotificationSide.STAFF,
                    role=UserRole.TENANT_STAFF,
                    scope=NotificationScope.INVOLVED,
                ),
            },
        ],
    )
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        order = (
            await session.execute(select(Order).where(Order.id == seeded["order"].id))
        ).scalar_one()
        order.assigned_to_user_id = seeded["staff"][0].id

    async with sm() as session:
        order = (
            await session.execute(select(Order).where(Order.id == seeded["order"].id))
        ).scalar_one()
        payloads = await build_order_submitted(
            session, tenant=demo_tenant, order=order, base_url=BASE_URL, settings=settings
        )

    assert _emails(payloads) == {"admin@4mex.cz"}


async def test_unassigned_order_reaches_narrow_scopes_too(
    owner_engine, demo_tenant, settings
) -> None:
    """An order nobody owns belongs to everyone.

    "Only orders assigned to me" must not quietly bin untriaged work just
    because one colleague happens to be on the wide setting — which is
    what the preference page promises in as many words.
    """
    from app.services.notification_service import build_order_submitted

    seeded = await _seed(
        owner_engine,
        demo_tenant.id,
        staff=[
            {"email": "admin@4mex.cz", "role": UserRole.TENANT_ADMIN},
            {
                "email": "operator@4mex.cz",
                "role": UserRole.TENANT_STAFF,
                "notification_prefs": _prefs(
                    side=NotificationSide.STAFF,
                    role=UserRole.TENANT_STAFF,
                    scope=NotificationScope.INVOLVED,
                ),
            },
        ],
    )
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        order = (
            await session.execute(select(Order).where(Order.id == seeded["order"].id))
        ).scalar_one()
        assert order.assigned_to_user_id is None
        payloads = await build_order_submitted(
            session, tenant=demo_tenant, order=order, base_url=BASE_URL, settings=settings
        )

    assert _emails(payloads) == {"admin@4mex.cz", "operator@4mex.cz"}


async def test_unassigned_order_still_reaches_everyone(owner_engine, demo_tenant, settings) -> None:
    """Relevance yields rather than dropping the event.

    With every staff member scoped to "only mine" and nobody assigned,
    a strict scope filter would silently bin the order. The preference
    page promises the opposite, in as many words.
    """
    from app.services.notification_service import build_order_submitted

    narrow = _prefs(
        side=NotificationSide.STAFF,
        role=UserRole.TENANT_STAFF,
        scope=NotificationScope.INVOLVED,
    )
    seeded = await _seed(
        owner_engine,
        demo_tenant.id,
        staff=[
            {"email": "a@4mex.cz", "notification_prefs": narrow},
            {"email": "b@4mex.cz", "notification_prefs": narrow},
        ],
    )
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        order = (
            await session.execute(select(Order).where(Order.id == seeded["order"].id))
        ).scalar_one()
        payloads = await build_order_submitted(
            session, tenant=demo_tenant, order=order, base_url=BASE_URL, settings=settings
        )

    assert _emails(payloads) == {"a@4mex.cz", "b@4mex.cz"}


async def test_assignee_hears_about_their_own_order(owner_engine, demo_tenant, settings) -> None:
    """A narrow scope still matches the order assigned to that person."""
    from app.services.notification_service import build_order_submitted

    narrow = _prefs(
        side=NotificationSide.STAFF,
        role=UserRole.TENANT_STAFF,
        scope=NotificationScope.INVOLVED,
    )
    seeded = await _seed(
        owner_engine,
        demo_tenant.id,
        staff=[
            {"email": "a@4mex.cz", "notification_prefs": narrow},
            {"email": "b@4mex.cz", "notification_prefs": narrow},
        ],
    )
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        order = (
            await session.execute(select(Order).where(Order.id == seeded["order"].id))
        ).scalar_one()
        order.assigned_to_user_id = seeded["staff"][0].id

    async with sm() as session:
        order = (
            await session.execute(select(Order).where(Order.id == seeded["order"].id))
        ).scalar_one()
        payloads = await build_order_submitted(
            session, tenant=demo_tenant, order=order, base_url=BASE_URL, settings=settings
        )

    assert _emails(payloads) == {"a@4mex.cz"}


# ---------------------------------------------------- reachability yields


async def test_accepted_contact_wins_over_pending_one(owner_engine, demo_tenant, settings) -> None:
    from app.services.notification_service import build_order_status_changed

    seeded = await _seed(
        owner_engine,
        demo_tenant.id,
        contacts=[
            {"email": "accepted@acme.cz", "role": CustomerContactRole.CUSTOMER_ADMIN},
            {"email": "pending@acme.cz", "accepted_at": None},
        ],
    )
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        order = (
            await session.execute(select(Order).where(Order.id == seeded["order"].id))
        ).scalar_one()
        payloads = await build_order_status_changed(
            session,
            tenant=demo_tenant,
            order=order,
            to_status=OrderStatus.READY,
            base_url=BASE_URL,
            settings=settings,
        )

    assert _emails(payloads) == {"accepted@acme.cz"}


async def test_pending_contact_is_told_when_nobody_else_can_be(
    owner_engine, demo_tenant, settings
) -> None:
    """Silence is the worse failure.

    A customer whose only contact has not accepted the invite yet still
    hears that their order moved — the link lands on a login page, which
    beats never being told at all.
    """
    from app.services.notification_service import build_order_status_changed

    seeded = await _seed(
        owner_engine,
        demo_tenant.id,
        contacts=[{"email": "pending@acme.cz", "accepted_at": None}],
    )
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        order = (
            await session.execute(select(Order).where(Order.id == seeded["order"].id))
        ).scalar_one()
        payloads = await build_order_status_changed(
            session,
            tenant=demo_tenant,
            order=order,
            to_status=OrderStatus.READY,
            base_url=BASE_URL,
            settings=settings,
        )

    assert _emails(payloads) == {"pending@acme.cz"}


# ------------------------------------------------------- contact scoping


async def test_uninvolved_contact_is_spared_the_broadcast(
    owner_engine, demo_tenant, settings
) -> None:
    """A customer with many contacts no longer copies all of them.

    ``customer_user`` defaults to ``involved``; ``customer_admin``
    defaults to ``all`` so the company still hears about everything.
    """
    from app.services.notification_service import build_order_status_changed

    seeded = await _seed(
        owner_engine,
        demo_tenant.id,
        contacts=[
            {"email": "boss@acme.cz", "role": CustomerContactRole.CUSTOMER_ADMIN},
            {"email": "creator@acme.cz"},
            {"email": "bystander@acme.cz"},
        ],
    )
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        order = (
            await session.execute(select(Order).where(Order.id == seeded["order"].id))
        ).scalar_one()
        order.created_by_contact_id = seeded["contacts"][1].id

    async with sm() as session:
        order = (
            await session.execute(select(Order).where(Order.id == seeded["order"].id))
        ).scalar_one()
        payloads = await build_order_status_changed(
            session,
            tenant=demo_tenant,
            order=order,
            to_status=OrderStatus.READY,
            base_url=BASE_URL,
            settings=settings,
        )

    assert _emails(payloads) == {"boss@acme.cz", "creator@acme.cz"}


# -------------------------------------------------------- tenant fallback


async def test_tenant_with_no_staff_falls_back_to_billing_email(
    owner_engine, demo_tenant, settings
) -> None:
    """An order submission must never vanish.

    With zero staff rows the old code returned ``None`` and the
    submission went unnotified.
    """
    from app.services.notification_service import build_order_submitted

    seeded = await _seed(owner_engine, demo_tenant.id, staff=[])
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        order = (
            await session.execute(select(Order).where(Order.id == seeded["order"].id))
        ).scalar_one()
        payloads = await build_order_submitted(
            session, tenant=demo_tenant, order=order, base_url=BASE_URL, settings=settings
        )

    assert _emails(payloads) == {demo_tenant.billing_email}


# ---------------------------------------------------------------- digest


def test_merge_for_digest_collapses_a_bulk_transition() -> None:
    """30 orders moved in one click is one email, not 30."""
    from app.services.notification_service import (
        DIGEST_TEMPLATE,
        OrderNotification,
        Recipient,
        merge_for_digest,
    )

    alice = Recipient(email="alice@4mex.cz", locale="cs")
    bob = Recipient(email="bob@4mex.cz", locale="en")

    def payload(recipient, number):
        return OrderNotification(
            event=NotificationEvent.ORDER_STATUS_CHANGED,
            recipient=recipient,
            tenant_name="4MEX",
            order_number=number,
            order_title=f"Job {number}",
            order_url=f"{BASE_URL}/app/orders/{number}",
            extra={"status_label": "Ready"},
        )

    merged = merge_for_digest(
        [payload(alice, "A1"), payload(bob, "B1"), payload(alice, "A2"), payload(alice, "A3")]
    )

    by_email = {m.recipient.email: m for m in merged}
    assert len(merged) == 2, "one mail per recipient, not per order"

    digest = by_email["alice@4mex.cz"]
    assert digest.template == DIGEST_TEMPLATE
    assert [o["number"] for o in digest.orders] == ["A1", "A2", "A3"]

    # A single order keeps the normal template — nothing changes for the
    # non-bulk path.
    assert by_email["bob@4mex.cz"].template == NotificationEvent.ORDER_STATUS_CHANGED.value


# ----------------------------------------------------------------- prefs


def test_gdpr_marker_does_not_break_prefs_parsing() -> None:
    """``gdpr_service`` writes into the same JSON column.

    Unknown top-level keys must be ignored, not raise, or an erased row
    would blow up every send that touches it.
    """
    prefs = NotificationPrefs.from_dict(
        {"_gdpr_erased_at": "2026-08-19T00:00:00+00:00"},
        side=NotificationSide.STAFF,
        role=UserRole.TENANT_ADMIN,
    )
    assert prefs.wants(NotificationEvent.ORDER_SUBMITTED)
    assert prefs.scope is NotificationScope.ALL


def test_unknown_event_names_are_ignored() -> None:
    """A pref written by a future release, read by an older one."""
    prefs = NotificationPrefs.from_dict(
        {"events": {"order_teleported": False, "order_comment": False}, "scope": "sideways"},
        side=NotificationSide.STAFF,
        role=UserRole.TENANT_ADMIN,
    )
    assert not prefs.wants(NotificationEvent.ORDER_COMMENT)
    assert prefs.wants(NotificationEvent.ORDER_SUBMITTED)
    assert prefs.scope is NotificationScope.ALL, "an unparseable scope falls back to the default"


def test_empty_prefs_resolve_to_role_defaults() -> None:
    """The rows that already exist in production are all ``{}``."""
    operator = NotificationPrefs.from_dict(
        {}, side=NotificationSide.STAFF, role=UserRole.TENANT_STAFF
    )
    assert operator.scope is NotificationScope.ALL
    assert operator.wants(NotificationEvent.ORDER_SUBMITTED)

    plain_contact = NotificationPrefs.from_dict(
        {}, side=NotificationSide.CONTACT, role=CustomerContactRole.CUSTOMER_USER
    )
    assert plain_contact.scope is NotificationScope.INVOLVED

    boss = NotificationPrefs.from_dict(
        {}, side=NotificationSide.CONTACT, role=CustomerContactRole.CUSTOMER_ADMIN
    )
    assert boss.scope is NotificationScope.ALL


def test_unticking_every_box_stores_an_empty_event_set() -> None:
    """Checkbox semantics: an absent name means off, not "unchanged"."""
    prefs = parse_form(
        side=NotificationSide.STAFF,
        role=UserRole.TENANT_ADMIN,
        selected_events=[],
        scope="all",
    )
    assert prefs.events == frozenset()
    assert not prefs.wants(NotificationEvent.ORDER_SUBMITTED)


def test_contact_prefs_cannot_hold_staff_only_events() -> None:
    """``order_assigned`` is an internal supplier concern."""
    prefs = parse_form(
        side=NotificationSide.CONTACT,
        role=CustomerContactRole.CUSTOMER_USER,
        selected_events=["order_assigned", "order_comment"],
        scope="involved",
    )
    assert prefs.events == frozenset({NotificationEvent.ORDER_COMMENT})


def test_prefs_round_trip_through_the_stored_dict() -> None:
    original = parse_form(
        side=NotificationSide.STAFF,
        role=UserRole.TENANT_STAFF,
        selected_events=["order_submitted", "order_assigned"],
        scope="involved",
    )
    restored = NotificationPrefs.from_dict(
        original.to_dict(), side=NotificationSide.STAFF, role=UserRole.TENANT_STAFF
    )
    assert restored == original


def test_prefs_helpers_read_the_row_role() -> None:
    """``prefs_for_user`` / ``prefs_for_contact`` pick the role default."""
    user = User(
        id=uuid4(),
        tenant_id=uuid4(),
        email="op@4mex.cz",
        full_name="Op",
        role=UserRole.TENANT_STAFF,
        notification_prefs={},
    )
    assert prefs_for_user(user).scope is NotificationScope.ALL

    contact = CustomerContact(
        id=uuid4(),
        tenant_id=uuid4(),
        customer_id=uuid4(),
        email="c@acme.cz",
        full_name="C",
        role=CustomerContactRole.CUSTOMER_USER,
        notification_prefs={},
    )
    assert prefs_for_contact(contact).scope is NotificationScope.INVOLVED


async def test_actor_cannot_take_a_tier_with_them(owner_engine, demo_tenant, settings) -> None:
    """The actor is dropped before the filters run, not after.

    An accepted contact comments on an order whose only other contact is
    still pending. Removing the actor at the end would let them satisfy
    the reachability filter on their own, take the tier with them, and
    leave nobody — silencing the one person who should hear about it.
    """
    from app.services.notification_service import build_order_comment

    seeded = await _seed(
        owner_engine,
        demo_tenant.id,
        contacts=[
            {"email": "author@acme.cz", "role": CustomerContactRole.CUSTOMER_ADMIN},
            {
                "email": "pending@acme.cz",
                "role": CustomerContactRole.CUSTOMER_ADMIN,
                "accepted_at": None,
            },
        ],
    )
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        order = (
            await session.execute(select(Order).where(Order.id == seeded["order"].id))
        ).scalar_one()
        payloads = await build_order_comment(
            session,
            tenant=demo_tenant,
            order=order,
            author_email="author@acme.cz",
            author_name="Autor",
            author_is_staff=False,
            body="Kdy to bude?",
            base_url=BASE_URL,
            settings=settings,
        )

    # The comment came from a contact, so it routes to staff — and the
    # contact-side check is the mirror image below.
    assert "author@acme.cz" not in _emails(payloads)

    async with sm() as session:
        order = (
            await session.execute(select(Order).where(Order.id == seeded["order"].id))
        ).scalar_one()
        staff_reply = await build_order_comment(
            session,
            tenant=demo_tenant,
            order=order,
            author_email="author@acme.cz",  # same address, now on the staff side
            author_name="Autor",
            author_is_staff=True,
            body="Zítra.",
            base_url=BASE_URL,
            settings=settings,
        )

    assert _emails(staff_reply) == {"pending@acme.cz"}


async def test_confirming_a_quote_makes_a_contact_involved(
    owner_engine, demo_tenant, settings
) -> None:
    """Accepting a quote is the most committing thing a contact can do.

    Involvement counted creators, commenters and uploaders but not the
    person who drove the transition, so a customer_user who confirmed a
    quote and did nothing else heard nothing more about it.
    """
    from app.services.notification_service import build_order_comment

    seeded = await _seed(
        owner_engine,
        demo_tenant.id,
        order_status=OrderStatus.CONFIRMED,
        contacts=[{"email": "signer@acme.cz"}, {"email": "bystander@acme.cz"}],
    )
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        session.add(
            OrderStatusHistory(
                id=uuid4(),
                tenant_id=demo_tenant.id,
                order_id=seeded["order"].id,
                from_status=OrderStatus.QUOTED,
                to_status=OrderStatus.CONFIRMED,
                changed_by_contact_id=seeded["contacts"][0].id,
            )
        )

    async with sm() as session:
        order = (
            await session.execute(select(Order).where(Order.id == seeded["order"].id))
        ).scalar_one()
        payloads = await build_order_comment(
            session,
            tenant=demo_tenant,
            order=order,
            author_email="staff0@4mex.cz",
            author_name="Staff",
            author_is_staff=True,
            body="Hotovo příští týden.",
            base_url=BASE_URL,
            settings=settings,
        )

    assert _emails(payloads) == {"signer@acme.cz"}


def test_assignment_notification_skips_an_unaccepted_assignee() -> None:
    """Reachability is soft everywhere there is an audience to fall back
    on. A single addressee has none, so here it stays hard."""
    from app.services.notification_service import build_order_assigned

    tenant_id = uuid4()
    pending = User(
        id=uuid4(),
        tenant_id=tenant_id,
        email="pending@4mex.cz",
        full_name="Pending",
        role=UserRole.TENANT_STAFF,
        password_hash=None,
        notification_prefs={},
    )
    order = Order(
        id=uuid4(),
        tenant_id=tenant_id,
        customer_id=uuid4(),
        number="2026-000002",
        title="Nope",
        status=OrderStatus.DRAFT,
    )

    @dataclass(frozen=True)
    class _Tenant:
        id: UUID
        name: str
        settings: dict

    assert (
        build_order_assigned(
            tenant=_Tenant(id=tenant_id, name="4MEX", settings={}),
            order=order,
            assignee=pending,
            base_url=BASE_URL,
            settings=None,
        )
        == []
    )
