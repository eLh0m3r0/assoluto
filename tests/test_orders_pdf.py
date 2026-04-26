"""End-to-end coverage for the ``GET /app/orders/{id}.pdf`` export.

The PDF renderer itself (``app.services.pdf_service``) is exercised
indirectly via the HTTP route — one smoke test against a seeded order
catches regressions in the whole stack (route, auth, service, font
registration) without having to poke at reportlab internals. A separate
pure-unit test (no Postgres) guards the renderer in isolation so the
coverage holds even when the DB-backed suite is skipped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.customer import Customer, CustomerContact
from app.models.enums import CustomerContactRole, OrderStatus, UserRole
from app.models.order import Order, OrderItem
from app.models.tenant import Tenant
from app.models.user import User
from app.security.passwords import hash_password
from app.services.pdf_service import format_money, render_order_pdf

# --------------------------------------------------------------------------
# Pure unit tests — no Postgres needed.
# --------------------------------------------------------------------------


def test_format_money_handles_none_and_currency() -> None:
    assert format_money(None) == ""
    assert format_money(Decimal("1234.5")) == "1234.50"
    assert format_money(Decimal("1234.5"), "CZK") == "1234.50 CZK"


def test_render_order_pdf_returns_valid_pdf_bytes() -> None:
    """Render a PDF directly (no app, no DB) and assert basic structure."""
    tenant_id = uuid4()
    tenant = Tenant(
        id=tenant_id,
        slug="4mex",
        name="4MEX s.r.o.",
        billing_email="b@b.cz",
        storage_prefix="tenants/4mex/",
    )
    customer = Customer(
        id=uuid4(),
        tenant_id=tenant_id,
        name="ACME s.r.o.",
        ico="12345678",
        dic="CZ12345678",
        billing_address={"street": "Hlavní 1", "city": "Praha", "zip": "110 00"},
    )
    order = Order(
        id=uuid4(),
        tenant_id=tenant_id,
        customer_id=customer.id,
        number="2026-000042",
        title="Zkouška — diakritika č ř š ž",
        status=OrderStatus.QUOTED,
        quoted_total=Decimal("3050.00"),
        currency="CZK",
    )
    order.created_at = datetime.now(UTC)
    order.submitted_at = datetime.now(UTC)
    order.promised_delivery_at = None

    items = [
        OrderItem(
            id=uuid4(),
            tenant_id=tenant_id,
            order_id=order.id,
            position=1,
            description="SKU-001 — Řezání plechu 3mm",
            quantity=Decimal("5"),
            unit="ks",
            unit_price=Decimal("250.00"),
            line_total=Decimal("1250.00"),
        ),
        OrderItem(
            id=uuid4(),
            tenant_id=tenant_id,
            order_id=order.id,
            position=2,
            description="Svařování nerez",
            quantity=Decimal("2"),
            unit="hod",
            unit_price=Decimal("800.00"),
            line_total=Decimal("1600.00"),
        ),
    ]

    pdf = render_order_pdf(order, items, customer, tenant, locale="cs")

    assert pdf.startswith(b"%PDF-1."), pdf[:20]
    assert len(pdf) > 1024, f"PDF suspiciously small: {len(pdf)} bytes"
    # A PDF always ends with %%EOF.
    assert b"%%EOF" in pdf[-32:]


# --------------------------------------------------------------------------
# Route-level tests — need the tenant_client + Postgres fixture.
# --------------------------------------------------------------------------


postgres_only = pytest.mark.postgres


async def _seed(owner_engine, tenant_id: UUID) -> dict:
    """Seed a staff user, ACME customer + contact, and a second Other customer + contact."""
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
        acme = Customer(
            id=uuid4(),
            tenant_id=tenant_id,
            name="ACME s.r.o.",
            ico="12345678",
            dic="CZ12345678",
            billing_address={"street": "Hlavní 1", "city": "Praha", "zip": "110 00"},
        )
        other = Customer(id=uuid4(), tenant_id=tenant_id, name="Other s.r.o.", ico="87654321")
        session.add_all([staff, acme, other])
        await session.flush()

        jan = CustomerContact(
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
        eva = CustomerContact(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=other.id,
            email="eva@other.cz",
            full_name="Eva",
            role=CustomerContactRole.CUSTOMER_USER,
            password_hash=hash_password("evapass"),
            invited_at=datetime.now(),
            accepted_at=datetime.now(),
        )
        session.add_all([jan, eva])
        await session.flush()

        return {"staff": staff, "acme": acme, "other": other, "jan": jan, "eva": eva}


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


async def _create_order_as_jan(client: AsyncClient) -> UUID:
    """Create + populate an order as jan@acme.cz; return its UUID."""
    create_resp = await client.post(
        "/app/orders",
        data={"title": "Diacritics test: kůň, řešení, žížala"},
        follow_redirects=False,
    )
    assert create_resp.status_code == 303
    order_id = UUID(create_resp.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0])

    # Add a couple of items so the PDF exercises the table branch.
    for desc, qty, unit, price in [
        ("SKU-001 — Řezání plechu 3mm", "5", "ks", "250.00"),
        ("SKU-002 — Svařování nerez", "2", "hod", "800.00"),
        ("Doprava Praha — Brno", "1", "ks", "500.00"),
    ]:
        data = {"description": desc, "quantity": qty, "unit": unit, "unit_price": price}
        r = await client.post(f"/app/orders/{order_id}/items", data=data, follow_redirects=False)
        assert r.status_code == 303, r.text

    return order_id


@postgres_only
async def test_pdf_export_smoke_for_staff(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)

    # Create a populated order as the contact, then switch to staff.
    await _login(tenant_client, "jan@acme.cz", "contactpass")
    order_id = await _create_order_as_jan(tenant_client)
    await _logout(tenant_client)

    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    resp = await tenant_client.get(f"/app/orders/{order_id}.pdf")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/pdf")
    # Inline disposition so the browser renders it in-place.
    assert "inline" in resp.headers.get("content-disposition", "")

    body = resp.content
    # Standard PDF magic number; version byte tolerated (1.3 to 1.7).
    assert body.startswith(b"%PDF-1."), body[:20]
    # A non-trivial order should produce >1 KB of output.
    assert len(body) > 1024, f"PDF suspiciously small: {len(body)} bytes"


@postgres_only
async def test_pdf_export_denied_for_other_customer_contact(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """Eva (Other s.r.o.) must not be able to download ACME's order PDF."""
    await _seed(owner_engine, demo_tenant.id)

    await _login(tenant_client, "jan@acme.cz", "contactpass")
    order_id = await _create_order_as_jan(tenant_client)
    await _logout(tenant_client)

    await _login(tenant_client, "eva@other.cz", "evapass")
    resp = await tenant_client.get(f"/app/orders/{order_id}.pdf")
    # Mirror the detail view — it returns 404, not 403.
    assert resp.status_code == 404


@postgres_only
async def test_pdf_export_contact_can_download_own_order(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """The order's own contact (Jan) can download the PDF of his order."""
    await _seed(owner_engine, demo_tenant.id)

    await _login(tenant_client, "jan@acme.cz", "contactpass")
    order_id = await _create_order_as_jan(tenant_client)

    resp = await tenant_client.get(f"/app/orders/{order_id}.pdf")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/pdf")
    assert resp.content.startswith(b"%PDF-1.")
