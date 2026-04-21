"""Tests for ``app.services.sla_service``.

Covers:
* ``on_time_rate`` bucketing (on-time / late / pending-overdue).
* ``pending`` counts separately from ``late`` — pending does not poison
  the on-time ratio.
* ``heatmap_data`` shape and tenant scoping via RLS.

All tests seed orders with the owner role (bypasses RLS), then exercise
the service via a portal_app session with ``app.tenant_id`` set — the
same codepath the FastAPI app uses.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.customer import Customer
from app.models.enums import OrderStatus
from app.models.order import Order
from app.models.tenant import Tenant
from app.services import sla_service

pytestmark = pytest.mark.postgres


APP_URL_DEFAULT = "postgresql+asyncpg://portal_app:portal_app@localhost:5432/portal"


async def _seed_customer(owner_engine, tenant_id, name: str = "ACME") -> Customer:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        cust = Customer(id=uuid4(), tenant_id=tenant_id, name=name, ico=None)
        session.add(cust)
        await session.flush()
        return cust


async def _seed_order(
    owner_engine,
    *,
    tenant_id,
    customer_id,
    number: str,
    promised: date | None,
    delivered: date | None,
    status: OrderStatus = OrderStatus.DRAFT,
) -> Order:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        order = Order(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=customer_id,
            number=number,
            title=f"Order {number}",
            status=status,
            promised_delivery_at=promised,
            delivered_at=delivered,
        )
        session.add(order)
        await session.flush()
        return order


async def _app_session(tenant_id):
    """Session as ``portal_app`` with the RLS tenant variable set."""
    url = os.environ.get("DATABASE_URL", APP_URL_DEFAULT)
    engine = create_async_engine(url, future=True)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    return engine, sm


async def test_on_time_rate_buckets_three_orders(owner_engine, demo_tenant) -> None:
    customer = await _seed_customer(owner_engine, demo_tenant.id)
    today = date.today()

    # 2 on-time
    await _seed_order(
        owner_engine,
        tenant_id=demo_tenant.id,
        customer_id=customer.id,
        number="2026-000001",
        promised=today - timedelta(days=10),
        delivered=today - timedelta(days=11),  # a day early
        status=OrderStatus.DELIVERED,
    )
    await _seed_order(
        owner_engine,
        tenant_id=demo_tenant.id,
        customer_id=customer.id,
        number="2026-000002",
        promised=today - timedelta(days=5),
        delivered=today - timedelta(days=5),  # same day = on time
        status=OrderStatus.DELIVERED,
    )
    # 1 late
    await _seed_order(
        owner_engine,
        tenant_id=demo_tenant.id,
        customer_id=customer.id,
        number="2026-000003",
        promised=today - timedelta(days=7),
        delivered=today - timedelta(days=3),  # 4 days late
        status=OrderStatus.DELIVERED,
    )

    engine, sm = await _app_session(demo_tenant.id)
    try:
        async with sm() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(demo_tenant.id)},
            )
            result = await sla_service.on_time_rate(
                session,
                date_from=today - timedelta(days=30),
                date_to=today,
            )
    finally:
        await engine.dispose()

    assert result["on_time"] == 2
    assert result["late"] == 1
    assert result["total"] == 3
    assert result["pending"] == 0
    # 2/3 ≈ 0.6666...
    assert abs(result["rate"] - (2 / 3)) < 1e-9


async def test_pending_does_not_inflate_late_or_rate(owner_engine, demo_tenant) -> None:
    """Pending = promised-past-but-not-delivered. Counted separately."""
    customer = await _seed_customer(owner_engine, demo_tenant.id)
    today = date.today()

    # 1 on-time delivered
    await _seed_order(
        owner_engine,
        tenant_id=demo_tenant.id,
        customer_id=customer.id,
        number="2026-000010",
        promised=today - timedelta(days=10),
        delivered=today - timedelta(days=10),
        status=OrderStatus.DELIVERED,
    )
    # 1 pending: promised in the past, not delivered
    await _seed_order(
        owner_engine,
        tenant_id=demo_tenant.id,
        customer_id=customer.id,
        number="2026-000011",
        promised=today - timedelta(days=4),
        delivered=None,
        status=OrderStatus.IN_PRODUCTION,
    )
    # 1 future-promised, not delivered — ignored entirely (not pending)
    await _seed_order(
        owner_engine,
        tenant_id=demo_tenant.id,
        customer_id=customer.id,
        number="2026-000012",
        promised=today + timedelta(days=10),
        delivered=None,
        status=OrderStatus.IN_PRODUCTION,
    )

    engine, sm = await _app_session(demo_tenant.id)
    try:
        async with sm() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(demo_tenant.id)},
            )
            result = await sla_service.on_time_rate(
                session,
                date_from=today - timedelta(days=30),
                date_to=today + timedelta(days=30),
            )
    finally:
        await engine.dispose()

    # rate over delivered only = 1/1 = 1.0, pending counted separately.
    assert result["on_time"] == 1
    assert result["late"] == 0
    assert result["total"] == 1
    assert result["pending"] == 1
    assert result["rate"] == pytest.approx(1.0)


async def test_heatmap_data_shape_and_tenant_scoped(owner_engine, demo_tenant) -> None:
    customer = await _seed_customer(owner_engine, demo_tenant.id, name="ACME")
    today = date.today()

    # Two orders for demo tenant in the same ISO week.
    await _seed_order(
        owner_engine,
        tenant_id=demo_tenant.id,
        customer_id=customer.id,
        number="2026-000020",
        promised=today - timedelta(days=14),
        delivered=today - timedelta(days=15),
        status=OrderStatus.DELIVERED,
    )
    await _seed_order(
        owner_engine,
        tenant_id=demo_tenant.id,
        customer_id=customer.id,
        number="2026-000021",
        promised=today - timedelta(days=14),
        delivered=today - timedelta(days=10),  # late
        status=OrderStatus.DELIVERED,
    )

    # Other tenant + order that must NOT leak into our results.
    sm_owner = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm_owner() as session, session.begin():
        other_tenant = Tenant(
            id=uuid4(),
            slug="other-sla",
            name="Other Tenant",
            billing_email="bill@other-sla.test",
            storage_prefix="tenants/other-sla/",
        )
        session.add(other_tenant)
        await session.flush()
        other_customer = Customer(id=uuid4(), tenant_id=other_tenant.id, name="Other Customer")
        session.add(other_customer)
        await session.flush()
        other_tenant_id = other_tenant.id
        other_customer_id = other_customer.id

    await _seed_order(
        owner_engine,
        tenant_id=other_tenant_id,
        customer_id=other_customer_id,
        number="2026-OTHER-1",
        promised=today - timedelta(days=14),
        delivered=today - timedelta(days=14),
        status=OrderStatus.DELIVERED,
    )

    engine, sm = await _app_session(demo_tenant.id)
    try:
        async with sm() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(demo_tenant.id)},
            )
            cells = await sla_service.heatmap_data(session, weeks=12)
    finally:
        await engine.dispose()

    # Exactly one customer, one week.
    assert len(cells) == 1
    cell = cells[0]
    assert cell["customer_name"] == "ACME"
    assert cell["total"] == 2
    assert cell["on_time"] == 1
    assert cell["late"] == 1
    assert isinstance(cell["week_start"], date)
    # No leakage from the other tenant.
    assert all(c["customer_name"] != "Other Customer" for c in cells)
