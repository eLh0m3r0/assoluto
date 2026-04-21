"""End-to-end hooks produce exactly one audit row per tracked action.

Each test invokes a service function and asserts a matching
``audit_events`` row appeared. The focus is on wiring — not on the
business logic, which has its own test file.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.audit_event import AuditEvent
from app.models.customer import Customer
from app.models.enums import OrderStatus, UserRole
from app.models.order import Order
from app.services import customer_service, product_service
from app.services.audit_service import ActorInfo
from app.services.order_service import (
    ActorRef,
    add_comment,
    add_item,
    remove_item,
    transition_order,
)

pytestmark = pytest.mark.postgres


async def _set_tenant(session, tenant_id: UUID) -> None:
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": str(tenant_id)},
    )


async def _seed_customer_and_order(owner_engine, tenant_id: UUID) -> tuple[UUID, UUID, UUID]:
    """Seed a staff user, a customer, and a DRAFT order. Returns
    ``(user_id, customer_id, order_id)`` — the user_id is used by helpers
    that need a real actor id (FK on ``order_status_history.changed_by_user_id``
    and ``order_comments.author_user_id``).
    """
    from app.models.user import User
    from app.security.passwords import hash_password

    owner_sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    user_id = uuid4()
    customer_id = uuid4()
    order_id = uuid4()
    async with owner_sm() as session, session.begin():
        session.add_all(
            [
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    email=f"audit-test-{user_id.hex[:8]}@4mex.cz",
                    full_name="Alice",
                    role=UserRole.TENANT_STAFF,
                    password_hash=hash_password("x" * 12),
                ),
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
    return user_id, customer_id, order_id


def _staff_actor(user_id: UUID | None = None) -> tuple[ActorRef, ActorInfo]:
    uid = user_id if user_id is not None else uuid4()
    ref = ActorRef(type="user", id=uid)
    info = ActorInfo(type="user", id=uid, label="Alice")
    return ref, info


async def test_order_transition_writes_audit_row(owner_engine, demo_tenant) -> None:
    from app.db.session import get_sessionmaker

    tenant_id = demo_tenant.id
    user_id, _, order_id = await _seed_customer_and_order(owner_engine, tenant_id)

    sm = get_sessionmaker()
    ref, info = _staff_actor(user_id=user_id)

    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_id)
        order = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
        await transition_order(
            session,
            order=order,
            to_status=OrderStatus.SUBMITTED,
            actor=ref,
            audit_actor=info,
        )

    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_id)
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.action == "order.status_changed")
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        ev = events[0]
        assert ev.entity_type == "order"
        assert ev.entity_id == order_id
        assert ev.diff == {"before": {"status": "draft"}, "after": {"status": "submitted"}}
        assert ev.actor_label == "Alice"


async def test_order_add_item_writes_audit_row(owner_engine, demo_tenant) -> None:
    from app.db.session import get_sessionmaker

    tenant_id = demo_tenant.id
    user_id, _, order_id = await _seed_customer_and_order(owner_engine, tenant_id)

    sm = get_sessionmaker()
    ref, info = _staff_actor(user_id=user_id)

    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_id)
        order = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
        await add_item(
            session,
            tenant_id=tenant_id,
            order=order,
            actor=ref,
            description="Widget",
            quantity=Decimal("2"),
            unit="ks",
            unit_price=Decimal("10.50"),
            audit_actor=info,
        )

    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_id)
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.action == "order.item_added")
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].entity_id == order_id


async def test_order_remove_item_writes_audit_row(owner_engine, demo_tenant) -> None:
    from app.db.session import get_sessionmaker
    from app.models.order import OrderItem

    tenant_id = demo_tenant.id
    user_id, _, order_id = await _seed_customer_and_order(owner_engine, tenant_id)

    sm = get_sessionmaker()
    ref, info = _staff_actor(user_id=user_id)

    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_id)
        order = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
        item = await add_item(
            session,
            tenant_id=tenant_id,
            order=order,
            actor=ref,
            description="Widget",
            quantity=Decimal("1"),
            audit_actor=info,
        )
        item_id = item.id

    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_id)
        order = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
        item = (
            await session.execute(select(OrderItem).where(OrderItem.id == item_id))
        ).scalar_one()
        await remove_item(session, order=order, item=item, actor=ref, audit_actor=info)

    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_id)
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.action == "order.item_removed")
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1


async def test_order_add_comment_writes_audit_row(owner_engine, demo_tenant) -> None:
    from app.db.session import get_sessionmaker

    tenant_id = demo_tenant.id
    user_id, _, order_id = await _seed_customer_and_order(owner_engine, tenant_id)

    sm = get_sessionmaker()
    ref, info = _staff_actor(user_id=user_id)

    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_id)
        order = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
        await add_comment(
            session,
            tenant_id=tenant_id,
            order=order,
            actor=ref,
            body="hello world",
            audit_actor=info,
        )

    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_id)
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.action == "order.comment_added")
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].diff["after"]["body"] == "hello world"


async def test_customer_create_update_delete_write_audit_rows(owner_engine, demo_tenant) -> None:
    from app.db.session import get_sessionmaker

    tenant_id = demo_tenant.id
    sm = get_sessionmaker()
    _, info = _staff_actor()

    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_id)
        customer = await customer_service.create_customer(
            session,
            tenant_id=tenant_id,
            name="Initial",
            ico="111",
            audit_actor=info,
        )
        cust_id = customer.id
        await customer_service.update_customer(
            session,
            customer,
            name="Renamed",
            ico="222",
            dic=None,
            notes=None,
            audit_actor=info,
        )

    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_id)
        customer = (
            await session.execute(select(Customer).where(Customer.id == cust_id))
        ).scalar_one()
        await customer_service.delete_customer(session, customer, audit_actor=info)

    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_id)
        actions = [
            r.action
            for r in (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.entity_type == "customer")
                    .order_by(AuditEvent.occurred_at)
                )
            )
            .scalars()
            .all()
        ]
        assert actions == ["customer.created", "customer.updated", "customer.deleted"]


async def test_product_create_update_write_audit_rows(owner_engine, demo_tenant) -> None:
    from app.db.session import get_sessionmaker
    from app.models.product import Product

    tenant_id = demo_tenant.id
    sm = get_sessionmaker()
    _, info = _staff_actor()

    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_id)
        product = await product_service.create_product(
            session,
            tenant_id=tenant_id,
            sku="SKU-1",
            name="Widget",
            default_price=Decimal("10.00"),
            audit_actor=info,
        )
        pid = product.id

    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_id)
        product = (await session.execute(select(Product).where(Product.id == pid))).scalar_one()
        await product_service.update_product(
            session,
            product,
            sku="SKU-1",
            name="Widget v2",
            description=None,
            unit="ks",
            default_price=Decimal("15.00"),
            customer_id=None,
            audit_actor=info,
        )

    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_id)
        events = (
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.entity_type == "product")
                    .order_by(AuditEvent.occurred_at)
                )
            )
            .scalars()
            .all()
        )
        assert [e.action for e in events] == ["product.created", "product.updated"]
        update_diff = events[1].diff
        assert "default_price" in update_diff["before"]
        assert update_diff["before"]["default_price"] == "10.00"
        assert update_diff["after"]["default_price"] == "15.00"


async def test_auth_invite_and_password_change_write_audit_rows(owner_engine, demo_tenant) -> None:
    from app.db.session import get_sessionmaker
    from app.models.user import User
    from app.security.passwords import hash_password
    from app.services.auth_service import change_user_password, invite_tenant_staff

    tenant_id = demo_tenant.id
    sm = get_sessionmaker()
    _, info = _staff_actor()

    # First: invite a staff member.
    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_id)
        await invite_tenant_staff(
            session,
            tenant_id=tenant_id,
            email="newstaff@4mex.cz",
            full_name="New Staff",
            role=UserRole.TENANT_STAFF,
            audit_actor=info,
        )

    # Then: create a user directly (bypassing invite) and change their password.
    owner_sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    user_id = uuid4()
    async with owner_sm() as session, session.begin():
        session.add(
            User(
                id=user_id,
                tenant_id=tenant_id,
                email="existing@4mex.cz",
                full_name="Existing Staff",
                role=UserRole.TENANT_STAFF,
                password_hash=hash_password("oldpass12"),
            )
        )

    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_id)
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        await change_user_password(
            session,
            user=user,
            current_password="oldpass12",
            new_password="newpass12",
            audit_actor=info,
        )

    async with sm() as session, session.begin():
        await _set_tenant(session, tenant_id)
        actions = [
            r.action
            for r in (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.entity_type == "user")
                    .order_by(AuditEvent.occurred_at)
                )
            )
            .scalars()
            .all()
        ]
        assert "user.invited" in actions
        assert "user.password_changed" in actions
