"""Tests for the global HTTP error handlers."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.customer import Customer, CustomerContact
from app.models.enums import CustomerContactRole, UserRole
from app.models.user import User
from app.security.passwords import hash_password

pytestmark = pytest.mark.postgres


@pytest.fixture
async def seeded_accounts(owner_engine, demo_tenant):
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        staff = User(
            id=uuid4(),
            tenant_id=demo_tenant.id,
            email="owner@4mex.cz",
            full_name="Owner",
            role=UserRole.TENANT_ADMIN,
            password_hash=hash_password("staffpass"),
        )
        customer = Customer(id=uuid4(), tenant_id=demo_tenant.id, name="ACME", ico="12345678")
        session.add_all([staff, customer])
        await session.flush()
        contact = CustomerContact(
            id=uuid4(),
            tenant_id=demo_tenant.id,
            customer_id=customer.id,
            email="jan@acme.cz",
            full_name="Jan",
            role=CustomerContactRole.CUSTOMER_USER,
            password_hash=hash_password("contactpass"),
            invited_at=datetime.now(),
            accepted_at=datetime.now(),
        )
        session.add(contact)
        await session.flush()
    return True


async def test_unauthenticated_html_redirects_to_login(
    tenant_client: AsyncClient, seeded_accounts
) -> None:
    """Hitting a protected page without a session redirects to /auth/login,
    not a raw JSON 401 response."""
    response = await tenant_client.get(
        "/app/orders",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/auth/login")


async def test_unauthenticated_json_still_gets_401(
    tenant_client: AsyncClient, seeded_accounts
) -> None:
    """API / JSON clients still get the structured 401 payload."""
    response = await tenant_client.get(
        "/app/orders",
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated"}


async def test_404_html_page_for_missing_order(tenant_client: AsyncClient, seeded_accounts) -> None:
    resp = await tenant_client.post(
        "/auth/login",
        data={"email": "owner@4mex.cz", "password": "staffpass"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    missing = "11111111-1111-1111-1111-111111111111"
    page = await tenant_client.get(f"/app/orders/{missing}", headers={"Accept": "text/html"})
    assert page.status_code == 404
    assert "Stránka nebyla nalezena" in page.text


async def test_dashboard_stats_include_orders_and_assets(
    tenant_client: AsyncClient, owner_engine, seeded_accounts
) -> None:
    """Dashboard should expose live counts (no longer placeholder)."""
    resp = await tenant_client.post(
        "/auth/login",
        data={"email": "owner@4mex.cz", "password": "staffpass"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    page = await tenant_client.get("/app")
    assert page.status_code == 200
    # Placeholder strings must be gone.
    assert "přijde v M2" not in page.text
    assert "přijde v M5" not in page.text
    # Three cards: Klienti, Otevřené objednávky, Majetek klientů.
    assert "Klienti" in page.text
    assert "Otevřené objednávky" in page.text
    assert "Aktivní majetek klientů" in page.text


async def test_asset_movement_accepts_reference_order_id(
    tenant_client: AsyncClient, owner_engine, seeded_accounts
) -> None:
    """The movement POST now exposes reference_order_id as a form field."""
    await tenant_client.post(
        "/auth/login",
        data={"email": "owner@4mex.cz", "password": "staffpass"},
        follow_redirects=False,
    )

    # Create a tiny order for the customer.
    await tenant_client.post("/auth/logout", follow_redirects=False)
    tenant_client.cookies.clear()
    await tenant_client.post(
        "/auth/login",
        data={"email": "jan@acme.cz", "password": "contactpass"},
        follow_redirects=False,
    )
    order_resp = await tenant_client.post(
        "/app/orders",
        data={"title": "Linked order"},
        follow_redirects=False,
    )
    order_id = order_resp.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0]

    # Switch back to staff, create asset, add movement referencing the order.
    await tenant_client.post("/auth/logout", follow_redirects=False)
    tenant_client.cookies.clear()
    await tenant_client.post(
        "/auth/login",
        data={"email": "owner@4mex.cz", "password": "staffpass"},
        follow_redirects=False,
    )

    # Seeded ACME uuid lookup via owner engine.
    from sqlalchemy import select

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        acme = (await session.execute(select(Customer).where(Customer.name == "ACME"))).scalar_one()

    create = await tenant_client.post(
        "/app/assets",
        data={
            "customer_id": str(acme.id),
            "code": "REF-TEST",
            "name": "Linked asset",
            "unit": "kg",
        },
        follow_redirects=False,
    )
    asset_id = create.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0]

    mv = await tenant_client.post(
        f"/app/assets/{asset_id}/movements",
        data={
            "type": "receive",
            "quantity": "100",
            "note": "linked",
            "reference_order_id": order_id,
        },
        follow_redirects=False,
    )
    assert mv.status_code == 303

    # Verify the link landed in the DB.
    from app.models.asset import AssetMovement

    async with sm() as session:
        rows = (
            (await session.execute(select(AssetMovement).where(AssetMovement.asset_id == asset_id)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert str(rows[0].reference_order_id) == order_id
