"""Tests for product-picker integration in the order item form."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.customer import Customer, CustomerContact
from app.models.enums import CustomerContactRole, UserRole
from app.models.order import OrderItem
from app.models.product import Product
from app.models.user import User
from app.security.passwords import hash_password

pytestmark = pytest.mark.postgres


async def _seed(owner_engine, tenant_id) -> dict:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        staff = User(
            id=uuid4(),
            tenant_id=tenant_id,
            email="owner@4mex.cz",
            full_name="Owner",
            role=UserRole.TENANT_ADMIN,
            password_hash=hash_password("ownerpass"),
        )
        acme = Customer(id=uuid4(), tenant_id=tenant_id, name="ACME", ico="11111111")
        session.add_all([staff, acme])
        await session.flush()

        contact = CustomerContact(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=acme.id,
            email="jan@acme.cz",
            full_name="Jan",
            role=CustomerContactRole.CUSTOMER_ADMIN,
            password_hash=hash_password("contactpass"),
            invited_at=datetime.now(),
            accepted_at=datetime.now(),
        )
        session.add(contact)

        shared = Product(
            tenant_id=tenant_id,
            sku="SKU-100",
            name="Plech Al 2mm",
            unit="kg",
            default_price=Decimal("85.00"),
        )
        private = Product(
            tenant_id=tenant_id,
            customer_id=acme.id,
            sku="ACME-001",
            name="Výkres",
            unit="ks",
            default_price=Decimal("1200.00"),
        )
        session.add_all([shared, private])
        await session.flush()

        return {
            "staff": staff,
            "acme": acme,
            "contact": contact,
            "shared_product": shared,
            "private_product": private,
        }


async def _login(client: AsyncClient, email: str, password: str) -> None:
    resp = await client.post(
        "/auth/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303


async def test_detail_page_exposes_shared_and_private_products(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "jan@acme.cz", "contactpass")

    create_resp = await tenant_client.post(
        "/app/orders", data={"title": "Pick demo"}, follow_redirects=False
    )
    order_id = UUID(create_resp.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0])

    detail = await tenant_client.get(f"/app/orders/{order_id}")
    assert detail.status_code == 200
    # Both products visible in the picker; OTH-only products would not be.
    assert "SKU-100" in detail.text
    assert "ACME-001" in detail.text
    # Free-text option is still there.
    assert "vlastní položka" in detail.text


async def test_add_item_with_product_id_inherits_defaults(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "jan@acme.cz", "contactpass")

    create_resp = await tenant_client.post(
        "/app/orders", data={"title": "Price demo"}, follow_redirects=False
    )
    order_id = UUID(create_resp.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0])

    add_resp = await tenant_client.post(
        f"/app/orders/{order_id}/items",
        data={
            "product_id": str(seed["private_product"].id),
            "quantity": "10",
            # description/unit/unit_price deliberately blank — server fills
            # from the catalog.
        },
        follow_redirects=False,
    )
    assert add_resp.status_code == 303

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        items = (
            (await session.execute(select(OrderItem).where(OrderItem.order_id == order_id)))
            .scalars()
            .all()
        )
    assert len(items) == 1
    item = items[0]
    assert item.product_id == seed["private_product"].id
    assert "ACME-001" in item.description
    assert item.unit == "ks"
    assert item.unit_price == Decimal("1200.00")


async def test_add_free_text_item_still_works(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "jan@acme.cz", "contactpass")

    create_resp = await tenant_client.post(
        "/app/orders", data={"title": "Free text"}, follow_redirects=False
    )
    order_id = UUID(create_resp.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0])

    add_resp = await tenant_client.post(
        f"/app/orders/{order_id}/items",
        data={"description": "Ručně napsaná položka", "quantity": "2", "unit": "hod"},
        follow_redirects=False,
    )
    assert add_resp.status_code == 303

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        items = (
            (await session.execute(select(OrderItem).where(OrderItem.order_id == order_id)))
            .scalars()
            .all()
        )
    assert len(items) == 1
    assert items[0].product_id is None
    assert items[0].description == "Ručně napsaná položka"
    assert items[0].unit == "hod"
