"""Sort by due date: unfinished work first, finished orders last (P3-10)."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.enums import OrderStatus
from app.models.order import Order
from app.services.order_service import ActorRef, build_orders_query
from tests.test_sla_service import _app_session

pytestmark = pytest.mark.postgres

SPECS = [
    # (number suffix, status, promised)
    ("delivered-aug", OrderStatus.DELIVERED, date(2026, 8, 3)),
    ("closed-aug", OrderStatus.CLOSED, date(2026, 8, 10)),
    ("cancelled-sep", OrderStatus.CANCELLED, date(2026, 9, 1)),
    ("open-late", OrderStatus.IN_PRODUCTION, date(2026, 11, 2)),
    ("open-soon", OrderStatus.CONFIRMED, date(2026, 10, 9)),
    ("open-overdue", OrderStatus.READY, date(2026, 10, 1)),
    ("open-undated", OrderStatus.SUBMITTED, None),
    ("closed-undated", OrderStatus.CLOSED, None),
]


async def _titles(tenant_id, sort: str) -> list[str]:
    engine, sm = await _app_session(tenant_id)
    try:
        async with sm() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
            )
            stmt = build_orders_query(actor=ActorRef(type="user", id=uuid4()), sort=sort)
            return [o.title for o in (await session.execute(stmt)).scalars().all()]
    finally:
        await engine.dispose()


async def _seed_orders(owner_engine, tenant_id) -> None:
    from tests.test_orders_item_autosave import _seed

    seed = await _seed(owner_engine, tenant_id)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        for n, (title, status, promised) in enumerate(SPECS, start=1):
            session.add(
                Order(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    customer_id=seed["acme"].id,
                    number=f"2026-{n:06d}",
                    title=title,
                    status=status,
                    promised_delivery_at=promised,
                )
            )


async def test_due_ascending_puts_finished_orders_last(owner_engine, demo_tenant) -> None:
    await _seed_orders(owner_engine, demo_tenant.id)
    assert await _titles(demo_tenant.id, "due") == [
        "open-overdue",
        "open-soon",
        "open-late",
        "open-undated",
        "delivered-aug",
        "closed-aug",
        "cancelled-sep",
        "closed-undated",
    ]


async def test_due_descending_still_keeps_finished_orders_last(owner_engine, demo_tenant) -> None:
    await _seed_orders(owner_engine, demo_tenant.id)
    assert await _titles(demo_tenant.id, "-due") == [
        "open-late",
        "open-soon",
        "open-overdue",
        "open-undated",
        "cancelled-sep",
        "closed-aug",
        "delivered-aug",
        "closed-undated",
    ]
