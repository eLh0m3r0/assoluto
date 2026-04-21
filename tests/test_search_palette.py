"""Tests for the ⌘K command palette (`/app/search`).

Covers:
- Staff can search across orders / customers / products in the tenant.
- Customer contacts are scoped to their own customer for orders and
  never see other customers or other-customer-dedicated products.
- Empty / too-short queries render an empty fragment, not an error.
- Response is HTML with the right structural markers.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.customer import Customer, CustomerContact
from app.models.enums import CustomerContactRole, OrderStatus, UserRole
from app.models.order import Order, OrderStatusHistory
from app.models.product import Product
from app.models.user import User
from app.security.passwords import hash_password

pytestmark = pytest.mark.postgres


async def _seed_fixtures(owner_engine, tenant_id: UUID) -> dict:
    """Seed a staff user, two customers (ACME + Widgets), a contact
    belonging to ACME, one order per customer, and three products:
    shared, ACME-only, Widgets-only.
    """
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        staff = User(
            id=uuid4(),
            tenant_id=tenant_id,
            email="staff@4mex.cz",
            full_name="Staff User",
            role=UserRole.TENANT_ADMIN,
            password_hash=hash_password("staffpass"),
        )
        acme = Customer(id=uuid4(), tenant_id=tenant_id, name="ACME Industries", ico="11111111")
        widgets = Customer(id=uuid4(), tenant_id=tenant_id, name="Widgets Co.", ico="22222222")
        session.add_all([staff, acme, widgets])
        await session.flush()

        contact = CustomerContact(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=acme.id,
            email="jan@acme.cz",
            full_name="Jan Novák",
            role=CustomerContactRole.CUSTOMER_ADMIN,
            password_hash=hash_password("contactpass"),
            invited_at=datetime.now(),
            accepted_at=datetime.now(),
        )
        session.add(contact)

        acme_order = Order(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=acme.id,
            number="2026-ACME001",
            title="Výroba panelů",
            status=OrderStatus.DRAFT,
            created_by_user_id=staff.id,
        )
        widgets_order = Order(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=widgets.id,
            number="2026-WID777",
            title="Svařování rámů",
            status=OrderStatus.DRAFT,
            created_by_user_id=staff.id,
        )
        session.add_all([acme_order, widgets_order])
        await session.flush()

        for o in (acme_order, widgets_order):
            session.add(
                OrderStatusHistory(
                    tenant_id=tenant_id,
                    order_id=o.id,
                    from_status=None,
                    to_status=o.status,
                    changed_by_user_id=staff.id,
                )
            )

        shared = Product(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=None,
            sku="SHARE-100",
            name="Sdílený plech",
            unit="ks",
            default_price=Decimal("99.50"),
            currency="CZK",
        )
        acme_product = Product(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=acme.id,
            sku="ACME-PRD",
            name="ACME speciální díl",
            unit="ks",
            default_price=Decimal("199"),
            currency="CZK",
        )
        widgets_product = Product(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=widgets.id,
            sku="WID-PRD",
            name="Widgets dedikovaný",
            unit="ks",
            default_price=Decimal("299"),
            currency="CZK",
        )
        session.add_all([shared, acme_product, widgets_product])
        await session.flush()

        return {
            "staff_id": staff.id,
            "acme_id": acme.id,
            "widgets_id": widgets.id,
            "contact_email": contact.email,
            "acme_order_id": acme_order.id,
            "widgets_order_id": widgets_order.id,
        }


async def _login(client: AsyncClient, email: str, password: str) -> None:
    resp = await client.post(
        "/auth/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


async def _logout(client: AsyncClient) -> None:
    await client.post("/auth/logout", follow_redirects=False)
    client.cookies.clear()


async def test_staff_searches_across_all_sections(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed_fixtures(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")

    # Order number match — reaches orders section.
    resp = await tenant_client.get("/app/search?q=ACME001")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    assert "2026-ACME001" in resp.text
    # Section header is rendered when there are order hits.
    assert ">Orders<" in resp.text or ">Objednávky<" in resp.text

    # Customer-name match — reaches customers section AND orders
    # section (Order JOIN matches customer name for staff).
    resp = await tenant_client.get("/app/search?q=Widgets")
    assert resp.status_code == 200
    assert "Widgets Co." in resp.text
    # Orders section heading present because the Widgets order matches
    # the customer-name filter on the JOIN.
    assert "2026-WID777" in resp.text

    # Product SKU match — reaches products section.
    resp = await tenant_client.get("/app/search?q=SHARE-100")
    assert resp.status_code == 200
    assert "SHARE-100" in resp.text


async def test_contact_never_sees_other_customers(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    seed = await _seed_fixtures(owner_engine, demo_tenant.id)
    await _login(tenant_client, "jan@acme.cz", "contactpass")

    # Searching for the OTHER customer's name:
    # - No "Clients" section for contacts (empty list).
    # - No Widgets order leaking in (contact's orders are scoped to ACME).
    resp = await tenant_client.get("/app/search?q=Widgets")
    assert resp.status_code == 200
    assert "Widgets Co." not in resp.text
    assert "2026-WID777" not in resp.text

    # Contact's OWN order should be findable by number.
    resp = await tenant_client.get("/app/search?q=ACME001")
    assert resp.status_code == 200
    assert "2026-ACME001" in resp.text

    # Products: shared + ACME-scoped are visible; Widgets-dedicated isn't.
    resp = await tenant_client.get("/app/search?q=PRD")
    assert resp.status_code == 200
    assert "ACME-PRD" in resp.text
    assert "WID-PRD" not in resp.text

    # Sanity: shared products are also visible.
    resp = await tenant_client.get("/app/search?q=SHARE")
    assert resp.status_code == 200
    assert "SHARE-100" in resp.text

    # Widgets order number string: contact looking for it must get nothing.
    resp = await tenant_client.get("/app/search?q=WID777")
    assert resp.status_code == 200
    assert "2026-WID777" not in resp.text
    assert str(seed["widgets_order_id"]) not in resp.text


async def test_short_and_empty_queries_return_empty_fragment(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed_fixtures(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")

    # Completely empty → 200 with HTML, no result anchors.
    resp = await tenant_client.get("/app/search?q=")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    assert "data-palette-item" not in resp.text

    # Missing query param at all → still 200.
    resp = await tenant_client.get("/app/search")
    assert resp.status_code == 200
    assert "data-palette-item" not in resp.text

    # One-character query → below threshold → empty.
    resp = await tenant_client.get("/app/search?q=a")
    assert resp.status_code == 200
    assert "data-palette-item" not in resp.text


async def test_nothing_found_message_is_rendered(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed_fixtures(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")

    resp = await tenant_client.get("/app/search?q=zzzzzzzznope")
    assert resp.status_code == 200
    # No result links in the empty-state fragment.
    assert "data-palette-item" not in resp.text
    # An empty-state message is shown rather than a blank panel.
    assert "zzzzzzzznope" in resp.text


async def test_unauthenticated_user_is_redirected(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed_fixtures(owner_engine, demo_tenant.id)
    # No login — the 401 handler bounces to /auth/login.
    resp = await tenant_client.get("/app/search?q=ACME", follow_redirects=False)
    assert resp.status_code in (303, 401)
