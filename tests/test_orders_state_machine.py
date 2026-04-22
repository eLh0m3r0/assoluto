"""State-machine enforcement for both staff and contacts.

Historically staff could teleport an order from any status to any other
(DRAFT → DELIVERED, CLOSED → DRAFT, …) because the service delegated
all access checks to the UI. Simulation found the UI happily exposed
every button — if an operator misclicked, SLA timestamps were skipped
and the order history became a nonsense chain.

We now run staff through ``STAFF_ALLOWED_TRANSITIONS``: forward one
step, one step back for corrections, cancel at any point, and
reopen-cancelled → DRAFT. Everything else raises
``ForbiddenTransition`` at the service layer — no way to bypass by
going straight to the POST endpoint.
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
    STAFF_ALLOWED_TRANSITIONS,
    ActorRef,
    ForbiddenTransition,
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


def test_staff_graph_has_every_status_as_a_key() -> None:
    """Every OrderStatus must appear as a key so ``.get(status, set())``
    never silently falls back to an empty frozenset that would render
    the order immutable for no reason."""
    assert set(STAFF_ALLOWED_TRANSITIONS.keys()) == set(OrderStatus)


def test_contact_graph_is_a_subset_of_staff_graph() -> None:
    """Any transition a contact can perform must also be available to
    staff — staff should never be *more* restricted than the customer."""
    for status, contact_targets in CONTACT_ALLOWED_TRANSITIONS.items():
        staff_targets = STAFF_ALLOWED_TRANSITIONS.get(status, set())
        assert contact_targets.issubset(staff_targets), (
            f"{status.value}: contacts can go to {contact_targets - staff_targets} "
            f"but staff cannot"
        )


@pytest.mark.postgres
async def test_staff_cannot_leap_draft_to_delivered(owner_engine, demo_tenant) -> None:
    """Core regression: before the fix, staff could skip the pipeline."""
    from sqlalchemy import select

    user, order = await _seed(owner_engine, demo_tenant.id, status=OrderStatus.DRAFT)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        fresh = (
            await session.execute(select(Order).where(Order.id == order.id))
        ).scalar_one()
        with pytest.raises(ForbiddenTransition):
            await transition_order(
                session,
                order=fresh,
                to_status=OrderStatus.DELIVERED,
                actor=ActorRef(type="user", id=user.id),
            )


@pytest.mark.postgres
async def test_staff_can_step_one_back_for_corrections(owner_engine, demo_tenant) -> None:
    """Staff noticed a typo in a SUBMITTED order → bounce back to DRAFT."""
    from sqlalchemy import select

    user, order = await _seed(owner_engine, demo_tenant.id, status=OrderStatus.SUBMITTED)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        fresh = (
            await session.execute(select(Order).where(Order.id == order.id))
        ).scalar_one()
        await transition_order(
            session,
            order=fresh,
            to_status=OrderStatus.DRAFT,
            actor=ActorRef(type="user", id=user.id),
        )
    async with sm() as session:
        got = (
            await session.execute(select(Order).where(Order.id == order.id))
        ).scalar_one()
        assert got.status == OrderStatus.DRAFT


@pytest.mark.postgres
async def test_staff_can_reopen_cancelled_into_draft(owner_engine, demo_tenant) -> None:
    from sqlalchemy import select

    user, order = await _seed(owner_engine, demo_tenant.id, status=OrderStatus.CANCELLED)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        fresh = (
            await session.execute(select(Order).where(Order.id == order.id))
        ).scalar_one()
        await transition_order(
            session,
            order=fresh,
            to_status=OrderStatus.DRAFT,
            actor=ActorRef(type="user", id=user.id),
        )


@pytest.mark.postgres
async def test_closed_is_near_terminal_only_delivered_reopen(
    owner_engine, demo_tenant
) -> None:
    """CLOSED can only step back to DELIVERED (for re-issue), nothing else."""
    from sqlalchemy import select

    user, order = await _seed(owner_engine, demo_tenant.id, status=OrderStatus.CLOSED)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        fresh = (
            await session.execute(select(Order).where(Order.id == order.id))
        ).scalar_one()
        with pytest.raises(ForbiddenTransition):
            await transition_order(
                session,
                order=fresh,
                to_status=OrderStatus.DRAFT,
                actor=ActorRef(type="user", id=user.id),
            )
