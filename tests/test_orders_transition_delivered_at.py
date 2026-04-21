"""Exercise the ``delivered_at`` side effect in ``transition_order``.

Semantics chosen (see docstring in ``order_service.transition_order``):

* Transitioning **into** ``OrderStatus.DELIVERED`` stamps
  ``delivered_at = date.today()`` **only if it is currently None** —
  staff re-entering DELIVERED after toggling away keeps the original
  delivery date so historical SLA numbers stay stable.
* Transitioning **away from** DELIVERED (e.g. back to READY) does NOT
  clear ``delivered_at`` — the original delivery happened and belongs
  in the audit trail / SLA view.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.enums import OrderStatus, UserRole
from app.models.order import Order
from app.models.user import User
from app.services.order_service import ActorRef, transition_order

pytestmark = pytest.mark.postgres


async def _seed(owner_engine, tenant_id):
    from app.models.customer import Customer

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        user = User(
            id=uuid4(),
            tenant_id=tenant_id,
            email="staff-sla@4mex.cz",
            full_name="Staff SLA",
            role=UserRole.TENANT_ADMIN,
            password_hash="x" * 20,
        )
        cust = Customer(id=uuid4(), tenant_id=tenant_id, name="Cust SLA")
        session.add_all([user, cust])
        await session.flush()
        order = Order(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=cust.id,
            number="2026-DLV-1",
            title="To be delivered",
            status=OrderStatus.READY,
        )
        session.add(order)
        await session.flush()
        return user, order


async def test_delivered_transition_sets_delivered_at(owner_engine, demo_tenant) -> None:
    user, order = await _seed(owner_engine, demo_tenant.id)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)

    assert order.delivered_at is None

    async with sm() as session, session.begin():
        # Reload inside this transaction.
        db_order = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        actor = ActorRef(type="user", id=user.id)
        await transition_order(
            session, order=db_order, to_status=OrderStatus.DELIVERED, actor=actor
        )

    async with sm() as session:
        db_order = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        assert db_order.status == OrderStatus.DELIVERED
        assert db_order.delivered_at == date.today()


async def test_delivered_at_is_not_cleared_on_transition_back(owner_engine, demo_tenant) -> None:
    user, order = await _seed(owner_engine, demo_tenant.id)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)

    actor = ActorRef(type="user", id=user.id)

    # Transition to DELIVERED — stamps date.
    async with sm() as session, session.begin():
        db_order = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        await transition_order(
            session, order=db_order, to_status=OrderStatus.DELIVERED, actor=actor
        )

    # Transition back to READY (staff can move anywhere). Must NOT clear
    # ``delivered_at``.
    async with sm() as session, session.begin():
        db_order = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        await transition_order(session, order=db_order, to_status=OrderStatus.READY, actor=actor)

    async with sm() as session:
        db_order = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        assert db_order.status == OrderStatus.READY
        assert db_order.delivered_at == date.today()

    # Re-enter DELIVERED: date must be unchanged (not re-stamped).
    # We simulate the "date drifts" case by pre-setting delivered_at to
    # something older and verifying the service keeps it.
    async with sm() as session, session.begin():
        db_order = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        from datetime import timedelta

        db_order.delivered_at = date.today() - timedelta(days=7)
        await session.flush()

    async with sm() as session, session.begin():
        db_order = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        await transition_order(
            session, order=db_order, to_status=OrderStatus.DELIVERED, actor=actor
        )

    async with sm() as session:
        db_order = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        assert db_order.status == OrderStatus.DELIVERED
        # Keeps the previously-recorded date (a week ago), does NOT reset
        # to today.
        assert db_order.delivered_at != date.today()
