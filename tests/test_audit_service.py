"""Unit + integration tests for the audit service primitives.

Pure helpers (``actor_from_principal``, ``diff_from_models``) run
without a database. The ``record()`` and ``list_events()`` tests need
a real Postgres and carry a ``postgres`` marker at the function level
so the pure unit tests stay available during offline development.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.audit_event import AuditEvent
from app.models.customer import Customer
from app.models.enums import OrderStatus
from app.models.order import Order
from app.services import audit_service
from app.services.audit_service import (
    ActorInfo,
    actor_from_principal,
    diff_from_models,
)

# Tests that touch the DB carry their own ``pytest.mark.postgres``. The
# pure-function tests above run without a running Postgres instance.


@dataclass
class _FakePrincipal:
    type: str
    id: UUID
    full_name: str
    email: str
    is_staff: bool = False
    customer_id: UUID | None = None


# ---------------------------------------------------------------------------
# actor_from_principal + diff_from_models
# ---------------------------------------------------------------------------


def test_actor_from_principal_none_is_system() -> None:
    actor = actor_from_principal(None)
    assert actor.type == "system"
    assert actor.id is None
    assert actor.label == "system"


def test_actor_from_principal_user() -> None:
    uid = uuid4()
    p = _FakePrincipal(type="user", id=uid, full_name="Jana Novák", email="j@x.cz")
    actor = actor_from_principal(p)
    assert actor.type == "user"
    assert actor.id == uid
    assert actor.label == "Jana Novák"


def test_actor_from_principal_contact_falls_back_to_email() -> None:
    cid = uuid4()
    p = _FakePrincipal(type="contact", id=cid, full_name="", email="c@x.cz")
    actor = actor_from_principal(p)
    assert actor.type == "contact"
    assert actor.label == "c@x.cz"


def test_diff_from_models_only_emits_changed_fields() -> None:
    before = type("X", (), {"a": 1, "b": "old", "c": 3})()
    after = type("X", (), {"a": 1, "b": "new", "c": 3})()
    diff = diff_from_models(before, after, ["a", "b", "c"])
    assert diff == {"before": {"b": "old"}, "after": {"b": "new"}}


def test_diff_from_models_empty_when_unchanged() -> None:
    before = type("X", (), {"a": 1, "b": 2})()
    after = type("X", (), {"a": 1, "b": 2})()
    assert diff_from_models(before, after, ["a", "b"]) == {}


def test_diff_from_models_coerces_non_json_types() -> None:
    uid_old = uuid4()
    uid_new = uuid4()
    ts_old = datetime(2025, 1, 1, tzinfo=UTC)
    before = type("X", (), {"uid": uid_old, "when": ts_old, "amount": Decimal("12.50")})()
    after = type(
        "X",
        (),
        {
            "uid": uid_new,
            "when": datetime(2026, 1, 1, tzinfo=UTC),
            "amount": Decimal("99.99"),
        },
    )()
    diff = diff_from_models(before, after, ["uid", "when", "amount"])
    assert diff["before"]["uid"] == str(uid_old)
    assert diff["after"]["uid"] == str(uid_new)
    assert diff["before"]["amount"] == "12.50"
    assert isinstance(diff["before"]["when"], str)


def test_diff_from_models_handles_missing_sides() -> None:
    after = type("X", (), {"a": 5})()
    diff = diff_from_models(None, after, ["a"])
    assert diff == {"before": {"a": None}, "after": {"a": 5}}
    diff2 = diff_from_models(after, None, ["a"])
    assert diff2 == {"before": {"a": 5}, "after": {"a": None}}


# ---------------------------------------------------------------------------
# record() writes a row in the caller's transaction
# ---------------------------------------------------------------------------


async def _set_tenant(session, tenant_id: UUID) -> None:
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": str(tenant_id)},
    )


@pytest.mark.postgres
async def test_record_writes_row_under_rls(owner_engine, demo_tenant) -> None:
    from app.db.session import get_sessionmaker

    sm = get_sessionmaker()
    tenant_id = demo_tenant.id
    entity_id = uuid4()

    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_id)
        actor = ActorInfo(type="user", id=uuid4(), label="Alice")
        event = await audit_service.record(
            session,
            action="customer.created",
            entity_type="customer",
            entity_id=entity_id,
            entity_label="ACME",
            actor=actor,
            after={"name": "ACME"},
        )
        assert event.id is not None
        assert event.tenant_id == tenant_id

    # Confirm persistence via a fresh session under the same tenant.
    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_id)
        from sqlalchemy import select

        rows = (await session.execute(select(AuditEvent))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.action == "customer.created"
        assert row.entity_id == entity_id
        assert row.diff == {"before": None, "after": {"name": "ACME"}}
        assert row.actor_type == "user"
        assert row.actor_label == "Alice"


# ---------------------------------------------------------------------------
# list_events scoping: staff vs contact
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_list_events_staff_sees_everything(owner_engine, demo_tenant) -> None:
    from app.db.session import get_sessionmaker

    sm = get_sessionmaker()
    owner_sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    tenant_id = demo_tenant.id

    # Seed a customer + order via the owner engine (bypasses RLS).
    async with owner_sm() as session, session.begin():
        customer = Customer(id=uuid4(), tenant_id=tenant_id, name="ACME")
        order = Order(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=customer.id,
            number="2026-000001",
            title="T",
            status=OrderStatus.DRAFT,
        )
        session.add_all([customer, order])

    # Write events as the app user.
    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_id)
        actor = ActorInfo(type="system", id=None, label="system")
        await audit_service.record(
            session,
            action="order.item_added",
            entity_type="order",
            entity_id=order.id,
            entity_label="2026-000001",
            actor=actor,
        )
        await audit_service.record(
            session,
            action="customer.updated",
            entity_type="customer",
            entity_id=customer.id,
            entity_label="ACME",
            actor=actor,
        )

    staff = _FakePrincipal(type="user", id=uuid4(), full_name="S", email="s@x.cz", is_staff=True)

    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_id)
        events, total = await audit_service.list_events(session, principal=staff)
        assert total == 2
        actions = {e.action for e in events}
        assert actions == {"order.item_added", "customer.updated"}


@pytest.mark.postgres
async def test_list_events_contact_sees_only_own_orders(owner_engine, demo_tenant) -> None:
    from app.db.session import get_sessionmaker

    sm = get_sessionmaker()
    owner_sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    tenant_id = demo_tenant.id

    customer_a_id = uuid4()
    customer_b_id = uuid4()
    order_a_id = uuid4()
    order_b_id = uuid4()

    async with owner_sm() as session, session.begin():
        session.add_all(
            [
                Customer(id=customer_a_id, tenant_id=tenant_id, name="A"),
                Customer(id=customer_b_id, tenant_id=tenant_id, name="B"),
                Order(
                    id=order_a_id,
                    tenant_id=tenant_id,
                    customer_id=customer_a_id,
                    number="2026-A-1",
                    title="A",
                    status=OrderStatus.DRAFT,
                ),
                Order(
                    id=order_b_id,
                    tenant_id=tenant_id,
                    customer_id=customer_b_id,
                    number="2026-B-1",
                    title="B",
                    status=OrderStatus.DRAFT,
                ),
            ]
        )

    actor = ActorInfo(type="system", id=None, label="system")
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
        await audit_service.record(
            session,
            action="customer.updated",
            entity_type="customer",
            entity_id=customer_a_id,
            entity_label="A",
            actor=actor,
        )

    # Contact of customer A sees only A's order event.
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
        events, total = await audit_service.list_events(session, principal=contact_a)
        assert total == 1
        assert events[0].entity_id == order_a_id
        assert events[0].entity_type == "order"
