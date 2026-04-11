"""Orders list filters, search, and pagination."""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.customer import Customer
from app.models.enums import OrderStatus, UserRole
from app.models.order import Order, OrderStatusHistory
from app.models.user import User
from app.security.passwords import hash_password

pytestmark = pytest.mark.postgres


async def _seed_many(owner_engine, tenant_id) -> dict:
    """Create staff + 2 customers + 25 orders spread across statuses."""
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
        other = Customer(id=uuid4(), tenant_id=tenant_id, name="Other", ico="22222222")
        session.add_all([staff, acme, other])
        await session.flush()

        orders = []
        for i in range(25):
            customer = acme if i % 2 == 0 else other
            status = OrderStatus.DRAFT if i < 18 else OrderStatus.DELIVERED
            title = f"Zakázka {i:02d}" if i != 7 else "Speciální hledaná"
            order = Order(
                id=uuid4(),
                tenant_id=tenant_id,
                customer_id=customer.id,
                number=f"2026-{i:06d}",
                title=title,
                status=status,
                created_by_user_id=staff.id,
            )
            session.add(order)
            orders.append(order)
        await session.flush()

        for o in orders:
            session.add(
                OrderStatusHistory(
                    tenant_id=tenant_id,
                    order_id=o.id,
                    from_status=None,
                    to_status=o.status,
                    changed_by_user_id=staff.id,
                )
            )
        return {"acme_id": acme.id, "other_id": other.id}


async def _login(client: AsyncClient, email: str, password: str) -> None:
    resp = await client.post(
        "/auth/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


async def test_pagination_default_page_size(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed_many(owner_engine, demo_tenant.id)
    await _login(tenant_client, "owner@4mex.cz", "ownerpass")

    page1 = await tenant_client.get("/app/orders")
    assert page1.status_code == 200
    # 20 rows on the first page, so the 25th entry must NOT be there.
    assert page1.text.count('onclick="location.href') == 20

    page2 = await tenant_client.get("/app/orders?page=2")
    assert page2.status_code == 200
    # 5 remaining rows on page 2.
    assert page2.text.count('onclick="location.href') == 5

    # Pagination controls present on page 1.
    assert "Další" in page1.text


async def test_status_filter_narrows_results(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed_many(owner_engine, demo_tenant.id)
    await _login(tenant_client, "owner@4mex.cz", "ownerpass")

    delivered = await tenant_client.get("/app/orders?status=delivered")
    assert delivered.status_code == 200
    rows = delivered.text.count('onclick="location.href')
    assert rows == 7  # 25 orders, indices 18..24 are DELIVERED


async def test_customer_filter_scopes_to_one_customer(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    seed = await _seed_many(owner_engine, demo_tenant.id)
    await _login(tenant_client, "owner@4mex.cz", "ownerpass")

    acme = await tenant_client.get(f"/app/orders?customer={seed['acme_id']}")
    rows = acme.text.count('onclick="location.href')
    # Even indices 0, 2, 4, ..., 24 -> 13 orders under ACME
    assert rows == 13
    assert "Other" not in acme.text or "Zakázka" not in acme.text.split("Other")[0]


async def test_search_by_title(tenant_client: AsyncClient, owner_engine, demo_tenant) -> None:
    await _seed_many(owner_engine, demo_tenant.id)
    await _login(tenant_client, "owner@4mex.cz", "ownerpass")

    resp = await tenant_client.get("/app/orders?q=Speci")
    rows = resp.text.count('onclick="location.href')
    assert rows == 1
    assert "Speciální hledaná" in resp.text
