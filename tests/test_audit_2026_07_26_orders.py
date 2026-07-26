"""Regression tests for the 2026-07-26 audit — orders, money, notifications.

See docs/audit-runs/2026-07-26-e2e/findings.md.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.customer import Customer, CustomerContact
from app.models.enums import OrderStatus, UserRole
from app.models.order import Order
from app.models.user import User
from app.security.passwords import hash_password
from app.services.order_service import ActorRef, add_item, transition_order

pytestmark = pytest.mark.postgres


async def _seed(owner_engine, tenant_id, *, status=OrderStatus.DRAFT):
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        user = User(
            id=uuid4(),
            tenant_id=tenant_id,
            email=f"staff-{uuid4().hex[:6]}@4mex.cz",
            full_name="Staff",
            role=UserRole.TENANT_ADMIN,
            password_hash=hash_password("x"),
        )
        cust = Customer(id=uuid4(), tenant_id=tenant_id, name="Acme")
        session.add_all([user, cust])
        await session.flush()
        contact = CustomerContact(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=cust.id,
            email=f"contact-{uuid4().hex[:6]}@acme.cz",
            full_name="Martina",
            password_hash=hash_password("x"),
        )
        order = Order(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=cust.id,
            number=f"2026-AU-{uuid4().hex[:4]}",
            title="Audit test",
            status=status,
        )
        session.add_all([contact, order])
        await session.flush()
        return user, cust, contact, order


# --------------------------------------------------------------- F-14


async def test_f14_add_item_refreshes_cached_quoted_total(owner_engine, demo_tenant) -> None:
    """The PDF prefers ``quoted_total`` over its own item table, so a
    stale cache prints a Subtotal that contradicts the lines above it.
    """
    user, _cust, _contact, order = await _seed(owner_engine, demo_tenant.id)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    actor = ActorRef(type="user", id=user.id)

    async with sm() as session, session.begin():
        fresh = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        await add_item(
            session,
            order=fresh,
            actor=actor,
            description="First line",
            quantity=Decimal("2"),
            unit="ks",
            unit_price=Decimal("500"),
            tenant_id=demo_tenant.id,
        )

    async with sm() as session:
        got = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        assert got.quoted_total == Decimal("1000")

    # Adding a second line must move the cached total too.
    async with sm() as session, session.begin():
        fresh = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        await add_item(
            session,
            order=fresh,
            actor=actor,
            description="Second line",
            quantity=Decimal("1"),
            unit="ks",
            unit_price=Decimal("500"),
            tenant_id=demo_tenant.id,
        )

    async with sm() as session:
        got = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        assert got.quoted_total == Decimal("1500"), "add_item must recompute the cached total"


# --------------------------------------------------------------- F-13


async def test_f13_unpriced_order_never_gets_a_fabricated_zero_quote(
    owner_engine, demo_tenant
) -> None:
    """Jumping past QUOTED on an order with no prices must leave the
    quote NULL, not stamp 0.00 onto the customer-facing PDF.
    """
    user, _cust, _contact, order = await _seed(owner_engine, demo_tenant.id)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    actor = ActorRef(type="user", id=user.id)

    async with sm() as session, session.begin():
        fresh = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        # An unpriced line: quantity but no unit_price.
        await add_item(
            session,
            order=fresh,
            actor=actor,
            description="Machining, price TBC",
            quantity=Decimal("3"),
            unit="ks",
            unit_price=None,
            tenant_id=demo_tenant.id,
        )

    async with sm() as session, session.begin():
        fresh = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        await transition_order(session, order=fresh, to_status=OrderStatus.CONFIRMED, actor=actor)

    async with sm() as session:
        got = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        assert got.status == OrderStatus.CONFIRMED
        assert got.quoted_total is None, "no priced line means no quote — not 0.00"


# --------------------------------------------------------------- contact confirms quote


async def test_contact_confirming_quote_notifies_the_supplier(
    owner_engine, demo_tenant, settings
) -> None:
    """The most commercially loaded click in the product.

    A contact moving QUOTED -> CONFIRMED used to build the notification
    from ``_contact_recipients``, so the supplier heard nothing and the
    customer was emailed about their own action.
    """
    from app.services.notification_service import build_order_status_changed

    user, _cust, contact, order = await _seed(
        owner_engine, demo_tenant.id, status=OrderStatus.QUOTED
    )
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)

    async with sm() as session:
        fresh = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        payload = await build_order_status_changed(
            session,
            tenant=demo_tenant,
            order=fresh,
            to_status=OrderStatus.CONFIRMED,
            base_url="https://4mex.example",
            settings=settings,
            actor_is_contact=True,
            actor_email=contact.email,
        )

    assert payload is not None, "the supplier must be told their quote was accepted"
    recipients = {email for email, _locale in payload.recipients_with_locale}
    assert user.email in recipients, "staff must receive it"
    assert contact.email not in recipients, "the actor must not be emailed their own click"


async def test_staff_status_change_still_notifies_the_customer(
    owner_engine, demo_tenant, settings
) -> None:
    """The opposite direction must keep working — staff -> contacts."""
    from app.services.notification_service import build_order_status_changed

    user, _cust, contact, order = await _seed(
        owner_engine, demo_tenant.id, status=OrderStatus.CONFIRMED
    )
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)

    async with sm() as session:
        fresh = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        payload = await build_order_status_changed(
            session,
            tenant=demo_tenant,
            order=fresh,
            to_status=OrderStatus.READY,
            base_url="https://4mex.example",
            settings=settings,
            actor_is_contact=False,
            actor_email=user.email,
        )

    assert payload is not None
    recipients = {email for email, _locale in payload.recipients_with_locale}
    assert contact.email in recipients
    assert user.email not in recipients
