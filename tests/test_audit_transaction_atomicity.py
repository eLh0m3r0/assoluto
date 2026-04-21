"""Audit rows must be atomic with the business mutation.

If the surrounding transaction is rolled back, the ``audit_events`` row
produced by :func:`audit_service.record` must disappear along with
whatever business state changed. That matters for two reasons:

- We never want an audit row claiming something happened when it
  didn't.
- We never want a successful mutation to silently skip its audit entry
  on rollback recovery.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.audit_event import AuditEvent
from app.models.customer import Customer
from app.models.enums import OrderStatus
from app.models.order import Order
from app.services.audit_service import ActorInfo
from app.services.order_service import ActorRef, add_item

pytestmark = pytest.mark.postgres


async def _set_tenant(session, tenant_id: UUID) -> None:
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": str(tenant_id)},
    )


async def test_audit_row_rolled_back_with_outer_transaction(owner_engine, demo_tenant) -> None:
    from app.db.session import get_sessionmaker

    tenant_id = demo_tenant.id
    owner_sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    customer_id = uuid4()
    order_id = uuid4()
    async with owner_sm() as session, session.begin():
        session.add_all(
            [
                Customer(id=customer_id, tenant_id=tenant_id, name="ACME"),
                Order(
                    id=order_id,
                    tenant_id=tenant_id,
                    customer_id=customer_id,
                    number="2026-000001",
                    title="T",
                    status=OrderStatus.DRAFT,
                ),
            ]
        )

    sm = get_sessionmaker()
    uid = uuid4()
    ref = ActorRef(type="user", id=uid)
    info = ActorInfo(type="user", id=uid, label="Alice")

    # Open a session; do the mutation + audit write; roll back explicitly.
    async with sm() as session:
        await session.execute(text("BEGIN"))
        await _set_tenant(session, tenant_id)
        order = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
        await add_item(
            session,
            tenant_id=tenant_id,
            order=order,
            actor=ref,
            description="Widget",
            quantity=Decimal("1"),
            audit_actor=info,
        )

        # Sanity: within the same transaction the row is visible.
        count = (await session.execute(select(AuditEvent))).scalars().all()
        assert len(count) == 1

        await session.execute(text("ROLLBACK"))

    # Fresh session: nothing persisted.
    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_id)
        rows = (await session.execute(select(AuditEvent))).scalars().all()
        assert rows == []
