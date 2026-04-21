"""Dashboard "Recent activity" feed — §7 of the Sprint-3 plan.

The feed is a thin view on top of ``audit_events`` (written by the §6
service hooks). It reuses ``audit_service._apply_principal_scope`` via
:func:`list_recent`, so these tests focus on:

* staff see every event in their tenant (cross-customer);
* customer contacts see only events on their own customer's orders —
  other customers' events must not leak;
* the order is ``occurred_at DESC``;
* cross-tenant RLS isolation — a second tenant's event never surfaces;
* the widget renders an empty state on a fresh tenant without crashing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.audit_event import AuditEvent
from app.models.customer import Customer
from app.models.enums import OrderStatus
from app.models.order import Order
from app.models.tenant import Tenant
from app.services import audit_service

pytestmark = pytest.mark.postgres


@dataclass
class _FakePrincipal:
    type: str
    id: UUID
    full_name: str
    email: str
    is_staff: bool = False
    customer_id: UUID | None = None


async def _set_tenant(session, tenant_id: UUID) -> None:
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": str(tenant_id)},
    )


async def _seed_two_customers_with_orders(
    owner_engine, tenant_id: UUID
) -> tuple[UUID, UUID, UUID, UUID]:
    """Insert two customers and one order each. Returns
    ``(customer_a_id, customer_b_id, order_a_id, order_b_id)``.
    """
    owner_sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    customer_a_id, customer_b_id = uuid4(), uuid4()
    order_a_id, order_b_id = uuid4(), uuid4()
    async with owner_sm() as session, session.begin():
        session.add_all(
            [
                Customer(id=customer_a_id, tenant_id=tenant_id, name="ACME-A"),
                Customer(id=customer_b_id, tenant_id=tenant_id, name="ACME-B"),
                Order(
                    id=order_a_id,
                    tenant_id=tenant_id,
                    customer_id=customer_a_id,
                    number="2026-A-1",
                    title="Order A",
                    status=OrderStatus.DRAFT,
                ),
                Order(
                    id=order_b_id,
                    tenant_id=tenant_id,
                    customer_id=customer_b_id,
                    number="2026-B-1",
                    title="Order B",
                    status=OrderStatus.DRAFT,
                ),
            ]
        )
    return customer_a_id, customer_b_id, order_a_id, order_b_id


# ---------------------------------------------------------------------------
# Service-layer tests (list_recent)
# ---------------------------------------------------------------------------


async def test_list_recent_staff_sees_cross_customer(owner_engine, demo_tenant) -> None:
    """Staff see events regardless of which customer they belong to."""
    from app.db.session import get_sessionmaker

    tenant_id = demo_tenant.id
    _, _, order_a_id, order_b_id = await _seed_two_customers_with_orders(owner_engine, tenant_id)

    sm = get_sessionmaker()
    actor = audit_service.ActorInfo(type="system", id=None, label="system")

    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_id)
        await audit_service.record(
            session,
            action="order.status_changed",
            entity_type="order",
            entity_id=order_a_id,
            entity_label="2026-A-1",
            actor=actor,
        )
        await audit_service.record(
            session,
            action="order.item_added",
            entity_type="order",
            entity_id=order_b_id,
            entity_label="2026-B-1",
            actor=actor,
        )

    staff = _FakePrincipal(type="user", id=uuid4(), full_name="S", email="s@x.cz", is_staff=True)
    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_id)
        events = await audit_service.list_recent(session, principal=staff, limit=20)

    actions = {e.action for e in events}
    assert actions == {"order.status_changed", "order.item_added"}


async def test_list_recent_contact_scoped_to_own_customer(owner_engine, demo_tenant) -> None:
    """Contact sees only events on orders of their own customer."""
    from app.db.session import get_sessionmaker

    tenant_id = demo_tenant.id
    customer_a_id, customer_b_id, order_a_id, order_b_id = await _seed_two_customers_with_orders(
        owner_engine, tenant_id
    )

    sm = get_sessionmaker()
    actor = audit_service.ActorInfo(type="system", id=None, label="system")

    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_id)
        await audit_service.record(
            session,
            action="order.status_changed",
            entity_type="order",
            entity_id=order_a_id,
            entity_label="2026-A-1",
            actor=actor,
        )
        await audit_service.record(
            session,
            action="order.status_changed",
            entity_type="order",
            entity_id=order_b_id,
            entity_label="2026-B-1",
            actor=actor,
        )
        # Customer-level event against A — a contact should NOT see it,
        # list_recent is scoped to entity_type='order' for contacts.
        await audit_service.record(
            session,
            action="customer.updated",
            entity_type="customer",
            entity_id=customer_a_id,
            entity_label="ACME-A",
            actor=actor,
        )

    # Contact of customer A sees only their own order event.
    contact_a = _FakePrincipal(
        type="contact",
        id=uuid4(),
        full_name="CA",
        email="a@x.cz",
        is_staff=False,
        customer_id=customer_a_id,
    )
    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_id)
        events = await audit_service.list_recent(session, principal=contact_a, limit=20)

    assert len(events) == 1
    assert events[0].entity_id == order_a_id
    # Explicitly confirm no leakage of B's order or the customer event.
    entity_ids = {e.entity_id for e in events}
    assert order_b_id not in entity_ids
    assert customer_b_id not in entity_ids


async def test_list_recent_ordered_by_occurred_at_desc(owner_engine, demo_tenant) -> None:
    """Newest event first."""
    from app.db.session import get_sessionmaker

    tenant_id = demo_tenant.id
    _, _, order_a_id, _ = await _seed_two_customers_with_orders(owner_engine, tenant_id)

    sm = get_sessionmaker()
    now = datetime.now(UTC)

    # Insert three events with explicit timestamps so ordering is
    # unambiguous regardless of clock resolution.
    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_id)
        for i, offset in enumerate(
            [timedelta(minutes=-30), timedelta(minutes=-10), timedelta(minutes=-20)]
        ):
            ev = AuditEvent(
                id=uuid4(),
                tenant_id=tenant_id,
                occurred_at=now + offset,
                actor_type="system",
                actor_id=None,
                actor_label="system",
                action="order.status_changed",
                entity_type="order",
                entity_id=order_a_id,
                entity_label=f"label-{i}",
            )
            session.add(ev)

    staff = _FakePrincipal(type="user", id=uuid4(), full_name="S", email="s@x.cz", is_staff=True)
    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_id)
        events = await audit_service.list_recent(session, principal=staff, limit=20)

    assert len(events) == 3
    # Strictly descending timestamps.
    for earlier, later in zip(events[1:], events[:-1], strict=True):
        assert later.occurred_at >= earlier.occurred_at


async def test_list_recent_empty_fresh_tenant(owner_engine, demo_tenant) -> None:
    """A tenant with no events returns an empty list — no crash."""
    from app.db.session import get_sessionmaker

    sm = get_sessionmaker()
    staff = _FakePrincipal(type="user", id=uuid4(), full_name="S", email="s@x.cz", is_staff=True)
    async with sm() as session, session.begin():
        await _set_tenant(session, demo_tenant.id)
        events = await audit_service.list_recent(session, principal=staff, limit=20)
    assert events == []


async def test_list_recent_respects_rls_cross_tenant(owner_engine, wipe_db) -> None:
    """A second tenant's event does not leak into the first tenant's feed."""
    from app.db.session import get_sessionmaker

    # Two tenants + one audit event each, seeded via the owner engine
    # so we sidestep RLS while writing.
    owner_sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    tenant_a_id = uuid4()
    tenant_b_id = uuid4()
    order_a_id = uuid4()
    order_b_id = uuid4()
    customer_a_id = uuid4()
    customer_b_id = uuid4()
    # Insert in dependency order — AuditEvent has no SQLAlchemy relationship
    # to Tenant, so unit-of-work can't infer FK ordering on its own.
    async with owner_sm() as session, session.begin():
        session.add_all(
            [
                Tenant(
                    id=tenant_a_id,
                    slug="alpha",
                    name="alpha s.r.o.",
                    billing_email="billing@alpha.cz",
                    storage_prefix="tenants/alpha/",
                ),
                Tenant(
                    id=tenant_b_id,
                    slug="beta",
                    name="beta s.r.o.",
                    billing_email="billing@beta.cz",
                    storage_prefix="tenants/beta/",
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                Customer(id=customer_a_id, tenant_id=tenant_a_id, name="A"),
                Customer(id=customer_b_id, tenant_id=tenant_b_id, name="B"),
                Order(
                    id=order_a_id,
                    tenant_id=tenant_a_id,
                    customer_id=customer_a_id,
                    number="2026-A-1",
                    title="A",
                    status=OrderStatus.DRAFT,
                ),
                Order(
                    id=order_b_id,
                    tenant_id=tenant_b_id,
                    customer_id=customer_b_id,
                    number="2026-B-1",
                    title="B",
                    status=OrderStatus.DRAFT,
                ),
                AuditEvent(
                    id=uuid4(),
                    tenant_id=tenant_a_id,
                    occurred_at=datetime.now(UTC),
                    actor_type="system",
                    actor_id=None,
                    actor_label="system",
                    action="order.status_changed",
                    entity_type="order",
                    entity_id=order_a_id,
                    entity_label="2026-A-1",
                ),
                AuditEvent(
                    id=uuid4(),
                    tenant_id=tenant_b_id,
                    occurred_at=datetime.now(UTC),
                    actor_type="system",
                    actor_id=None,
                    actor_label="system",
                    action="order.status_changed",
                    entity_type="order",
                    entity_id=order_b_id,
                    entity_label="2026-B-1",
                ),
            ]
        )

    sm = get_sessionmaker()
    staff = _FakePrincipal(type="user", id=uuid4(), full_name="S", email="s@x.cz", is_staff=True)

    # Session scoped to tenant A must not see B's event.
    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_a_id)
        events = await audit_service.list_recent(session, principal=staff, limit=20)
    assert len(events) == 1
    assert events[0].entity_id == order_a_id


# ---------------------------------------------------------------------------
# Router / template integration — empty state renders and does not crash
# ---------------------------------------------------------------------------


async def test_dashboard_renders_empty_activity_widget(
    tenant_client, demo_tenant, owner_engine
) -> None:
    """A logged-in staff user on a fresh tenant sees the empty-state copy."""
    from app.models.enums import UserRole
    from app.models.user import User
    from app.security.passwords import hash_password

    # Seed a staff user so we can log in via the stack.
    owner_sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with owner_sm() as session, session.begin():
        session.add(
            User(
                id=uuid4(),
                tenant_id=demo_tenant.id,
                email="staff@4mex.cz",
                full_name="Staff Member",
                password_hash=hash_password("hunter22!"),
                role=UserRole.TENANT_STAFF,
                is_active=True,
            )
        )

    # Log in. The CsrfAwareClient handles CSRF for the POST.
    resp = await tenant_client.post(
        "/auth/login",
        data={"email": "staff@4mex.cz", "password": "hunter22!"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    # Fetch the dashboard and check the widget + empty state are present.
    resp = await tenant_client.get("/app/", follow_redirects=False)
    assert resp.status_code == 200
    body = resp.text
    assert "Recent activity" in body or "Nedávná" in body or "Poslední aktivita" in body
    # Empty-state sentinel substring (matches either EN or the CZ
    # translation). If no translation exists yet the EN string shows.
    assert (
        "No activity yet" in body
        or "actions will show up here" in body
        or "Zatím" in body  # CZ plausible fallback if translator picks it up
    )
