"""Tests for the product catalog: CRUD + customer scoping + autocomplete."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.customer import Customer
from app.models.enums import UserRole
from app.models.user import User
from app.security.passwords import hash_password

pytestmark = pytest.mark.postgres


async def _seed_staff_and_two_customers(owner_engine, tenant_id: UUID) -> dict:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        staff = User(
            id=uuid4(),
            tenant_id=tenant_id,
            email="staff@4mex.cz",
            full_name="Staff",
            role=UserRole.TENANT_ADMIN,
            password_hash=hash_password("staffpass"),
        )
        acme = Customer(id=uuid4(), tenant_id=tenant_id, name="ACME", ico="11111111")
        other = Customer(id=uuid4(), tenant_id=tenant_id, name="Other", ico="22222222")
        session.add_all([staff, acme, other])
        await session.flush()
        return {"staff": staff, "acme": acme, "other": other}


async def _login(client: AsyncClient, email: str, password: str) -> None:
    resp = await client.post(
        "/auth/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


async def test_catalog_crud_happy_path(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    seed = await _seed_staff_and_two_customers(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")

    # Create a shared product.
    resp = await tenant_client.post(
        "/app/products",
        data={
            "sku": "SKU-100",
            "name": "Plech 2mm",
            "unit": "ks",
            "default_price": "99.50",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    # Create a customer-scoped product.
    resp = await tenant_client.post(
        "/app/products",
        data={
            "sku": "ACME-001",
            "name": "Custom výkres",
            "unit": "ks",
            "customer_id": str(seed["acme"].id),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    # Catalog page lists both.
    listing = await tenant_client.get("/app/products")
    assert listing.status_code == 200
    assert "SKU-100" in listing.text
    assert "ACME-001" in listing.text
    assert "Plech 2mm" in listing.text
    assert "(sdílené)" in listing.text
    assert "ACME" in listing.text


async def test_duplicate_sku_is_rejected(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed_staff_and_two_customers(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")

    await tenant_client.post(
        "/app/products",
        data={"sku": "DUP", "name": "First", "unit": "ks"},
        follow_redirects=False,
    )
    resp = await tenant_client.post(
        "/app/products",
        data={"sku": "DUP", "name": "Second", "unit": "ks"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "už existuje" in resp.text


async def test_search_scopes_to_customer(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    seed = await _seed_staff_and_two_customers(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")

    # 2 shared + 1 ACME-specific + 1 Other-specific
    for data in [
        {"sku": "SH-1", "name": "Shared One", "unit": "ks"},
        {"sku": "SH-2", "name": "Shared Two", "unit": "ks"},
        {
            "sku": "ACME-A",
            "name": "ACME Alpha",
            "unit": "ks",
            "customer_id": str(seed["acme"].id),
        },
        {
            "sku": "OTH-A",
            "name": "Other Alpha",
            "unit": "ks",
            "customer_id": str(seed["other"].id),
        },
    ]:
        await tenant_client.post("/app/products", data=data, follow_redirects=False)

    # Search with ACME context — should return shared + ACME-specific, NOT other.
    resp = await tenant_client.get(f"/app/products/search?q=a&customer_id={seed['acme'].id}")
    assert resp.status_code == 200
    payload = resp.json()
    skus = sorted(r["sku"] for r in payload["results"])
    # "a" matches "Shared", "ACME Alpha" by name, and OTH-A by name. But
    # OTH-A is filtered by customer scope.
    assert "ACME-A" in skus
    assert "OTH-A" not in skus

    # No customer scope — everything matches.
    resp_all = await tenant_client.get("/app/products/search?q=a")
    skus_all = sorted(r["sku"] for r in resp_all.json()["results"])
    assert "ACME-A" in skus_all
    assert "OTH-A" in skus_all


async def test_contacts_cannot_access_catalog(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """Catalog is staff-only; contacts get 403."""
    from datetime import datetime

    from app.models.customer import CustomerContact
    from app.models.enums import CustomerContactRole

    seed = await _seed_staff_and_two_customers(owner_engine, demo_tenant.id)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        session.add(
            CustomerContact(
                id=uuid4(),
                tenant_id=demo_tenant.id,
                customer_id=seed["acme"].id,
                email="jan@acme.cz",
                full_name="Jan",
                role=CustomerContactRole.CUSTOMER_ADMIN,
                password_hash=hash_password("contactpass"),
                invited_at=datetime.now(),
                accepted_at=datetime.now(),
            )
        )

    await _login(tenant_client, "jan@acme.cz", "contactpass")
    resp = await tenant_client.get("/app/products")
    assert resp.status_code == 403
