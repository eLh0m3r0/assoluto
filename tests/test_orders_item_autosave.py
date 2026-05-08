"""Tests for field-level autosave of DRAFT order items (roadmap §4b).

Covers the HTMX ``POST/PATCH /app/orders/{id}/items/{item_id}/patch`` route
and the backing ``order_service.update_item`` domain function. The happy
path is a quantity change on a DRAFT order — the response body is the
row fragment containing the new quantity and a "Saved" flash. The
non-DRAFT case short-circuits with a 409 and a localized error block.
"""

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
from app.models.user import User
from app.security.csrf import CSRF_COOKIE_NAME
from app.security.passwords import hash_password

pytestmark = pytest.mark.postgres


async def _seed(owner_engine, tenant_id: UUID) -> dict:
    """Create one staff user, two customers, and one accepted contact for ACME."""
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
        acme = Customer(id=uuid4(), tenant_id=tenant_id, name="ACME", ico="12345678")
        other = Customer(id=uuid4(), tenant_id=tenant_id, name="Other", ico="87654321")
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


async def _create_draft_with_item(
    client: AsyncClient, owner_engine, *, customer_id: UUID
) -> tuple[UUID, UUID]:
    """Create a DRAFT order as the currently-logged-in principal and add one item."""
    create = await client.post(
        "/app/orders",
        data={"title": "Autosave test", "customer_id": str(customer_id)},
        follow_redirects=False,
    )
    assert create.status_code == 303, create.text
    order_id = UUID(create.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0])

    add = await client.post(
        f"/app/orders/{order_id}/items",
        data={
            "description": "Plech",
            "quantity": "3",
            "unit": "ks",
            "unit_price": "100",
        },
        follow_redirects=False,
    )
    assert add.status_code == 303, add.text

    # Fetch the only item's id via the owner engine (bypasses RLS).
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        item = (
            await session.execute(select(OrderItem).where(OrderItem.order_id == order_id))
        ).scalar_one()
    return order_id, item.id


async def test_staff_autosave_quantity_on_draft(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """Staff PATCHes quantity on a DRAFT — row returned with the new value."""
    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    order_id, item_id = await _create_draft_with_item(
        tenant_client, owner_engine, customer_id=seed["acme"].id
    )

    resp = await tenant_client.post(
        f"/app/orders/{order_id}/items/{item_id}/patch",
        data={"quantity": "7", "unit_price": "100", "note": ""},
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    # The row carries the item's id so HTMX knows what to swap; the new
    # quantity must be present and no error marker.
    assert f"item-row-{item_id}" in body
    assert 'value="7"' in body
    assert "data-row-error" not in body
    assert "data-row-saved" in body

    # Verify DB actually changed and quoted_total recomputed.
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        reloaded = (
            await session.execute(select(OrderItem).where(OrderItem.id == item_id))
        ).scalar_one()
        assert reloaded.quantity == Decimal("7")
        assert reloaded.line_total == Decimal("700.00")


async def test_staff_can_autosave_on_submitted_and_quoted(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """Staff editing prices on a SUBMITTED / QUOTED order MUST succeed —
    that's the supplier-side quoting workflow. Items become locked only
    after CONFIRMED."""
    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    order_id, item_id = await _create_draft_with_item(
        tenant_client, owner_engine, customer_id=seed["acme"].id
    )

    # DRAFT → SUBMITTED. Staff updates the unit price.
    t = await tenant_client.post(
        f"/app/orders/{order_id}/transitions/submitted", follow_redirects=False
    )
    assert t.status_code == 303, t.text
    resp = await tenant_client.post(
        f"/app/orders/{order_id}/items/{item_id}/patch",
        data={"quantity": "3", "unit_price": "250", "note": ""},
    )
    assert resp.status_code == 200, resp.text
    assert "data-row-error" not in resp.text

    # SUBMITTED → QUOTED. Staff still allowed.
    t = await tenant_client.post(
        f"/app/orders/{order_id}/transitions/quoted", follow_redirects=False
    )
    assert t.status_code == 303, t.text
    resp = await tenant_client.post(
        f"/app/orders/{order_id}/items/{item_id}/patch",
        data={"quantity": "5", "unit_price": "275", "note": ""},
    )
    assert resp.status_code == 200, resp.text
    assert "data-row-error" not in resp.text

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        reloaded = (
            await session.execute(select(OrderItem).where(OrderItem.id == item_id))
        ).scalar_one()
        assert reloaded.quantity == Decimal("5")
        assert reloaded.unit_price == Decimal("275")


async def test_autosave_blocked_on_confirmed_returns_409(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """Past QUOTED the order is contractually agreed — items become
    append-only and price-locked even for staff. 409 + inline error."""
    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    order_id, item_id = await _create_draft_with_item(
        tenant_client, owner_engine, customer_id=seed["acme"].id
    )

    # Walk DRAFT → SUBMITTED → QUOTED → CONFIRMED.
    for status_path in ("submitted", "quoted", "confirmed"):
        t = await tenant_client.post(
            f"/app/orders/{order_id}/transitions/{status_path}", follow_redirects=False
        )
        assert t.status_code == 303, t.text

    resp = await tenant_client.post(
        f"/app/orders/{order_id}/items/{item_id}/patch",
        data={"quantity": "99", "unit_price": "100", "note": ""},
    )
    assert resp.status_code == 409
    assert "data-row-error" in resp.text

    # DB must remain unchanged.
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        reloaded = (
            await session.execute(select(OrderItem).where(OrderItem.id == item_id))
        ).scalar_one()
        assert reloaded.quantity == Decimal("3")


async def test_autosave_invalid_quantity_renders_error_fragment(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """Negative quantity: 200 with the error fragment (no DB change)."""
    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    order_id, item_id = await _create_draft_with_item(
        tenant_client, owner_engine, customer_id=seed["acme"].id
    )

    resp = await tenant_client.post(
        f"/app/orders/{order_id}/items/{item_id}/patch",
        data={"quantity": "-5", "unit_price": "100", "note": ""},
    )
    # 200 with the row + error (so HTMX still swaps in place and user sees hint).
    assert resp.status_code == 200, resp.text
    assert "data-row-error" in resp.text
    assert "data-row-saved" not in resp.text

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        reloaded = (
            await session.execute(select(OrderItem).where(OrderItem.id == item_id))
        ).scalar_one()
        assert reloaded.quantity == Decimal("3")


async def test_autosave_authz_contact_from_other_customer_404(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """A contact from Other s.r.o. must not patch an item on an ACME order."""
    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "jan@acme.cz", "contactpass")
    order_id, item_id = await _create_draft_with_item(
        tenant_client, owner_engine, customer_id=seed["acme"].id
    )

    await _logout(tenant_client)
    await _login(tenant_client, "eva@other.cz", "evapass")

    resp = await tenant_client.post(
        f"/app/orders/{order_id}/items/{item_id}/patch",
        data={"quantity": "9", "unit_price": "100", "note": ""},
    )
    # Matches the existing detail-route behaviour: cross-customer access
    # looks like "order doesn't exist" to preserve non-enumerability.
    assert resp.status_code == 404


async def test_autosave_rejects_bad_csrf_header(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """Mismatched X-CSRF-Token header must fail closed with 403."""
    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    order_id, item_id = await _create_draft_with_item(
        tenant_client, owner_engine, customer_id=seed["acme"].id
    )

    # Send the PATCH bypassing the CsrfAwareClient auto-injection by
    # explicitly overriding the header with a wrong token. No form body
    # so the fallback form-field path can't rescue it either.
    assert tenant_client.cookies.get(CSRF_COOKIE_NAME)  # cookie exists
    resp = await tenant_client.post(
        f"/app/orders/{order_id}/items/{item_id}/patch",
        headers={"X-CSRF-Token": "definitely-wrong"},
    )
    assert resp.status_code == 403
