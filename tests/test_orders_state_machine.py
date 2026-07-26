"""State-machine enforcement for both staff and contacts.

Staff may move an order to **any** status. That freedom was previously
withheld — the graph only allowed one step forward or back — because
leaping across the pipeline skipped the milestone side effects and left
orders out of the SLA report. The restriction treated the symptom; the
cause is now fixed in ``_backfill_milestones``, so the graph is open and
these tests pin the *replacement* guarantee: a jump must arrive with its
milestone data filled in.

Customer contacts stay on the tight ``CONTACT_ALLOWED_TRANSITIONS``
graph, enforced at the service layer so posting straight to the endpoint
gains nothing.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.customer import Customer
from app.models.enums import OrderStatus, UserRole
from app.models.order import Order
from app.models.user import User
from app.security.passwords import hash_password
from app.services.order_service import (
    CONTACT_ALLOWED_TRANSITIONS,
    PIPELINE,
    STAFF_ALLOWED_TRANSITIONS,
    ActorRef,
    ForbiddenTransition,
    pipeline_rank,
    skipped_statuses,
    transition_order,
)


async def _seed(owner_engine, tenant_id, *, status: OrderStatus):
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        user = User(
            id=uuid4(),
            tenant_id=tenant_id,
            email=f"owner-{uuid4().hex[:6]}@4mex.cz",
            full_name="Owner",
            role=UserRole.TENANT_ADMIN,
            password_hash=hash_password("x"),
        )
        cust = Customer(id=uuid4(), tenant_id=tenant_id, name="C")
        session.add_all([user, cust])
        await session.flush()
        order = Order(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=cust.id,
            number=f"2026-SM-{uuid4().hex[:4]}",
            title="SM test",
            status=status,
        )
        session.add(order)
        await session.flush()
        return user, order


# --------------------------------------------------------------- graph shape


def test_staff_graph_has_every_status_as_a_key() -> None:
    """Every OrderStatus must appear as a key so ``.get(status, set())``
    never silently falls back to an empty frozenset that would render
    the order immutable for no reason."""
    assert set(STAFF_ALLOWED_TRANSITIONS.keys()) == set(OrderStatus)


def test_staff_may_reach_every_other_status() -> None:
    """The operator graph is fully connected — minus the self-loop,
    which ``transition_order`` rejects as "already in that status"."""
    for status, targets in STAFF_ALLOWED_TRANSITIONS.items():
        assert targets == set(OrderStatus) - {status}


def test_contact_graph_is_a_subset_of_staff_graph() -> None:
    """Any transition a contact can perform must also be available to
    staff — staff should never be *more* restricted than the customer."""
    for status, contact_targets in CONTACT_ALLOWED_TRANSITIONS.items():
        staff_targets = STAFF_ALLOWED_TRANSITIONS.get(status, set())
        assert contact_targets.issubset(staff_targets), (
            f"{status.value}: contacts can go to {contact_targets - staff_targets} but staff cannot"
        )


def test_cancelled_is_off_pipeline() -> None:
    """CANCELLED must not have a rank — callers branch on ``None`` to
    avoid treating a cancellation as "moved back to draft"."""
    assert pipeline_rank(OrderStatus.CANCELLED) is None
    assert OrderStatus.CANCELLED not in PIPELINE
    assert [pipeline_rank(s) for s in PIPELINE] == list(range(len(PIPELINE)))


# ------------------------------------------------------------ skipped steps


def test_skipped_statuses_lists_the_jumped_over_steps() -> None:
    assert skipped_statuses(OrderStatus.DRAFT, OrderStatus.CONFIRMED) == [
        OrderStatus.SUBMITTED,
        OrderStatus.QUOTED,
    ]


def test_adjacent_and_backward_moves_skip_nothing() -> None:
    assert skipped_statuses(OrderStatus.DRAFT, OrderStatus.SUBMITTED) == []
    assert skipped_statuses(OrderStatus.DELIVERED, OrderStatus.DRAFT) == []


def test_cancelling_skips_nothing_but_reopening_counts_the_prefix() -> None:
    """Moving *to* CANCELLED leaves the pipeline; moving *from* it lands
    somewhere on the pipeline and everything before that point counts as
    unobserved (the backfill only fills blanks, so genuinely-recorded
    stamps survive)."""
    assert skipped_statuses(OrderStatus.READY, OrderStatus.CANCELLED) == []
    assert skipped_statuses(OrderStatus.CANCELLED, OrderStatus.QUOTED) == [
        OrderStatus.DRAFT,
        OrderStatus.SUBMITTED,
    ]


# ------------------------------------------------------------ staff freedom


@pytest.mark.postgres
async def test_staff_can_leap_draft_to_delivered_with_stamps_backfilled(
    owner_engine, demo_tenant
) -> None:
    """The headline change: a jump is allowed, and it arrives complete.

    ``delivered_at`` in particular must be set — ``sla_service`` filters
    on ``delivered_at IS NOT NULL``, so an order that skipped the stamp
    would vanish from the on-time report entirely.
    """
    from sqlalchemy import select, text

    user, order = await _seed(owner_engine, demo_tenant.id, status=OrderStatus.DRAFT)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": str(demo_tenant.id)},
        )
        fresh = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        await transition_order(
            session,
            order=fresh,
            to_status=OrderStatus.DELIVERED,
            actor=ActorRef(type="user", id=user.id),
        )

    async with sm() as session:
        got = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        assert got.status == OrderStatus.DELIVERED
        assert got.submitted_at is not None, "submitted_at must be backfilled on a jump"
        assert got.delivered_at is not None, "delivered_at drives the SLA report"
        # This order has no line items at all, so there is nothing to
        # price and ``quoted_total`` must stay NULL. It used to be
        # stamped 0.00 by a ``coalesce(sum(...), 0)``, and the invoice
        # PDF prefers that cached value over its own item table — so an
        # unpriced order printed "0 Kč" to the customer, which reads as
        # "free" rather than "not quoted yet" (audit 2026-07-26, F-13).
        assert got.quoted_total is None, "an order with no priced items has no quote"


@pytest.mark.postgres
async def test_backfill_never_overwrites_an_existing_stamp(owner_engine, demo_tenant) -> None:
    """A real observed timestamp outranks a best-effort backfilled one."""
    from datetime import UTC, date, datetime

    from sqlalchemy import select, text

    original_submitted = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    original_delivered = date(2026, 1, 9)

    user, order = await _seed(owner_engine, demo_tenant.id, status=OrderStatus.DRAFT)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": str(demo_tenant.id)},
        )
        fresh = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        fresh.submitted_at = original_submitted
        fresh.delivered_at = original_delivered
        await session.flush()
        await transition_order(
            session,
            order=fresh,
            to_status=OrderStatus.CLOSED,
            actor=ActorRef(type="user", id=user.id),
        )

    async with sm() as session:
        got = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        assert got.submitted_at == original_submitted
        assert got.delivered_at == original_delivered


@pytest.mark.postgres
async def test_cancelling_does_not_backfill_pipeline_stamps(owner_engine, demo_tenant) -> None:
    """CANCELLED is off-pipeline — a cancelled draft was never delivered."""
    from sqlalchemy import select, text

    user, order = await _seed(owner_engine, demo_tenant.id, status=OrderStatus.DRAFT)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": str(demo_tenant.id)},
        )
        fresh = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        await transition_order(
            session,
            order=fresh,
            to_status=OrderStatus.CANCELLED,
            actor=ActorRef(type="user", id=user.id),
        )

    async with sm() as session:
        got = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        assert got.cancelled_at is not None
        assert got.delivered_at is None
        assert got.submitted_at is None


@pytest.mark.postgres
async def test_staff_can_step_one_back_for_corrections(owner_engine, demo_tenant) -> None:
    """Staff noticed a typo in a SUBMITTED order → bounce back to DRAFT."""
    from sqlalchemy import select, text

    user, order = await _seed(owner_engine, demo_tenant.id, status=OrderStatus.SUBMITTED)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        # Audit-event RLS is FORCE-ed; even as the owner we need
        # ``app.tenant_id`` set before the transition triggers an
        # ``audit_events`` INSERT.
        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": str(demo_tenant.id)},
        )
        fresh = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        await transition_order(
            session,
            order=fresh,
            to_status=OrderStatus.DRAFT,
            actor=ActorRef(type="user", id=user.id),
        )
    async with sm() as session:
        got = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        assert got.status == OrderStatus.DRAFT


@pytest.mark.postgres
async def test_staff_can_reopen_cancelled_into_any_status(owner_engine, demo_tenant) -> None:
    """Reopening is no longer forced through DRAFT — an order cancelled
    by mistake while in production goes straight back to production."""
    from sqlalchemy import select, text

    user, order = await _seed(owner_engine, demo_tenant.id, status=OrderStatus.CANCELLED)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": str(demo_tenant.id)},
        )
        fresh = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        await transition_order(
            session,
            order=fresh,
            to_status=OrderStatus.IN_PRODUCTION,
            actor=ActorRef(type="user", id=user.id),
        )
    async with sm() as session:
        got = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        assert got.status == OrderStatus.IN_PRODUCTION


@pytest.mark.postgres
async def test_staff_cannot_transition_to_the_same_status(owner_engine, demo_tenant) -> None:
    """The one move that stays forbidden — it would write a no-op row
    into the status history and fire a pointless notification email."""
    from sqlalchemy import select

    user, order = await _seed(owner_engine, demo_tenant.id, status=OrderStatus.READY)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        fresh = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        with pytest.raises(ForbiddenTransition):
            await transition_order(
                session,
                order=fresh,
                to_status=OrderStatus.READY,
                actor=ActorRef(type="user", id=user.id),
            )


# ---------------------------------------------------------- contact limits


@pytest.mark.postgres
async def test_contact_still_cannot_leap_the_pipeline(owner_engine, demo_tenant) -> None:
    """Freedom is an operator privilege — customers keep the tight graph
    so they cannot mark their own order delivered."""
    from sqlalchemy import select

    _user, order = await _seed(owner_engine, demo_tenant.id, status=OrderStatus.DRAFT)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        fresh = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        with pytest.raises(ForbiddenTransition):
            await transition_order(
                session,
                order=fresh,
                to_status=OrderStatus.DELIVERED,
                actor=ActorRef(
                    type="contact",
                    id=uuid4(),
                    customer_id=fresh.customer_id,
                ),
            )
