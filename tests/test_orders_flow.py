"""End-to-end tests for the orders state machine and workflow.

Drives the real app through the full happy path (DRAFT -> SUBMITTED ->
QUOTED -> CONFIRMED -> IN_PRODUCTION -> READY -> DELIVERED -> CLOSED),
covers forbidden-transition guards, and cross-tenant / cross-customer
access denials via RLS + ACL.
"""

from __future__ import annotations

import re
from datetime import datetime
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.customer import Customer, CustomerContact
from app.models.enums import CustomerContactRole, OrderStatus, UserRole
from app.models.order import Order, OrderItem
from app.models.user import User
from app.security.passwords import hash_password

pytestmark = pytest.mark.postgres


async def _seed_everyone(owner_engine, tenant_id: UUID) -> dict:
    """Seed a staff user, a customer, and an accepted contact."""
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
        customer = Customer(id=uuid4(), tenant_id=tenant_id, name="ACME", ico="12345678")
        session.add_all([staff, customer])
        await session.flush()

        contact = CustomerContact(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=customer.id,
            email="jan@acme.cz",
            full_name="Jan Novák",
            role=CustomerContactRole.CUSTOMER_ADMIN,
            password_hash=hash_password("contactpass"),
            invited_at=datetime.now(),
            accepted_at=datetime.now(),
        )
        session.add(contact)
        await session.flush()

        return {
            "staff": staff,
            "customer": customer,
            "contact": contact,
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


async def _order_row(owner_engine, order_id) -> Order:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        return (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()


async def test_full_order_lifecycle(tenant_client: AsyncClient, owner_engine, demo_tenant) -> None:
    seed = await _seed_everyone(owner_engine, demo_tenant.id)

    # ----------------------------------- 1) contact creates DRAFT
    await _login(tenant_client, "jan@acme.cz", "contactpass")

    create_resp = await tenant_client.post(
        "/app/orders",
        data={"title": "Zakázka 01", "notes": ""},
        follow_redirects=False,
    )
    assert create_resp.status_code == 303
    order_url = create_resp.headers["location"]
    order_id = UUID(order_url.rsplit("/", 1)[-1].split("?", 1)[0])

    # Order number should be "<year>-000001"
    order = await _order_row(owner_engine, order_id)
    assert order.status == OrderStatus.DRAFT
    assert order.number.endswith("-000001")
    assert order.customer_id == seed["customer"].id

    # ----------------------------------- 2) contact adds 2 items
    add_resp = await tenant_client.post(
        f"/app/orders/{order_id}/items",
        data={"description": "Řezání plechu", "quantity": "5", "unit": "ks"},
        follow_redirects=False,
    )
    assert add_resp.status_code == 303

    add_resp2 = await tenant_client.post(
        f"/app/orders/{order_id}/items",
        data={"description": "Svařování", "quantity": "2", "unit": "hod"},
        follow_redirects=False,
    )
    assert add_resp2.status_code == 303

    # ----------------------------------- 3) contact submits
    submit_resp = await tenant_client.post(
        f"/app/orders/{order_id}/transitions/submitted", follow_redirects=False
    )
    assert submit_resp.status_code == 303

    order = await _order_row(owner_engine, order_id)
    assert order.status == OrderStatus.SUBMITTED
    assert order.submitted_at is not None

    # Contact cannot edit items once submitted.
    locked_add = await tenant_client.post(
        f"/app/orders/{order_id}/items",
        data={"description": "Extra", "quantity": "1", "unit": "ks"},
        follow_redirects=False,
    )
    assert locked_add.status_code == 409

    # ----------------------------------- 4) staff logs in, prices items, quotes
    await _logout(tenant_client)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")

    # Staff can still edit items in SUBMITTED — add a priced line to round it out.
    await tenant_client.post(
        f"/app/orders/{order_id}/items",
        data={
            "description": "Doprava",
            "quantity": "1",
            "unit": "ks",
            "unit_price": "500",
        },
        follow_redirects=False,
    )

    # Quoting with the contact's two lines still unpriced is refused
    # (audit 2026-10-03 LOGIC-7) — the customer would otherwise confirm
    # a total that silently ignores them.
    early_quote = await tenant_client.post(
        f"/app/orders/{order_id}/transitions/quoted", follow_redirects=False
    )
    assert early_quote.status_code == 303
    assert "error=" in early_quote.headers["location"]
    assert (await _order_row(owner_engine, order_id)).status == OrderStatus.SUBMITTED

    # Staff prices the contact's lines through the inline autosave.
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        item_ids = (
            (
                await session.execute(
                    select(OrderItem.id)
                    .where(OrderItem.order_id == order_id, OrderItem.unit_price.is_(None))
                    .order_by(OrderItem.position)
                )
            )
            .scalars()
            .all()
        )
    assert len(item_ids) == 2
    for item_id, price in ((item_ids[0], "100"), (item_ids[1], "400")):
        patch = await tenant_client.post(
            f"/app/orders/{order_id}/items/{item_id}/patch", data={"unit_price": price}
        )
        assert patch.status_code == 200

    quote_resp = await tenant_client.post(
        f"/app/orders/{order_id}/transitions/quoted", follow_redirects=False
    )
    assert quote_resp.status_code == 303

    order = await _order_row(owner_engine, order_id)
    assert order.status == OrderStatus.QUOTED
    assert order.quoted_total is not None
    # 5 x 100 + 2 x 400 + 500
    assert float(order.quoted_total) == 1800.00

    # Contact cannot quote.
    await _logout(tenant_client)
    await _login(tenant_client, "jan@acme.cz", "contactpass")
    bad_transition = await tenant_client.post(
        f"/app/orders/{order_id}/transitions/quoted", follow_redirects=False
    )
    # Order is already in QUOTED; trying to re-quote is also forbidden.
    assert bad_transition.status_code == 409

    # ----------------------------------- 5) contact confirms the amount shown
    confirm_resp = await tenant_client.post(
        f"/app/orders/{order_id}/transitions/confirmed",
        data={"expected_total": "1800.00"},
        follow_redirects=False,
    )
    assert confirm_resp.status_code == 303
    order = await _order_row(owner_engine, order_id)
    assert order.status == OrderStatus.CONFIRMED
    assert float(order.confirmed_total) == 1800.00

    # ----------------------------------- 6) staff walks to DELIVERED
    await _logout(tenant_client)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")

    for status in ("in_production", "ready", "delivered", "closed"):
        resp = await tenant_client.post(
            f"/app/orders/{order_id}/transitions/{status}",
            follow_redirects=False,
        )
        assert resp.status_code == 303, (status, resp.text)

    order = await _order_row(owner_engine, order_id)
    assert order.status == OrderStatus.CLOSED
    assert order.closed_at is not None


async def test_contact_cannot_see_other_customers_orders(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed_everyone(owner_engine, demo_tenant.id)

    # Create a second customer + its contact
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        other_cust = Customer(
            id=uuid4(), tenant_id=demo_tenant.id, name="Other s.r.o.", ico="87654321"
        )
        session.add(other_cust)
        await session.flush()

        other_contact = CustomerContact(
            id=uuid4(),
            tenant_id=demo_tenant.id,
            customer_id=other_cust.id,
            email="eva@other.cz",
            full_name="Eva",
            role=CustomerContactRole.CUSTOMER_USER,
            password_hash=hash_password("evapass"),
            invited_at=datetime.now(),
            accepted_at=datetime.now(),
        )
        session.add(other_contact)
        await session.flush()

    # Jan creates an order for ACME.
    await _login(tenant_client, "jan@acme.cz", "contactpass")
    create_resp = await tenant_client.post(
        "/app/orders",
        data={"title": "Jen pro ACME"},
        follow_redirects=False,
    )
    acme_order_id = UUID(create_resp.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0])

    # Eva logs in and should not see Jan's order, and should 404 on direct access.
    await _logout(tenant_client)
    await _login(tenant_client, "eva@other.cz", "evapass")

    list_resp = await tenant_client.get("/app/orders")
    assert list_resp.status_code == 200
    assert "Jen pro ACME" not in list_resp.text

    detail_resp = await tenant_client.get(f"/app/orders/{acme_order_id}")
    assert detail_resp.status_code == 404


async def test_staff_internal_comment_hidden_from_contact(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed_everyone(owner_engine, demo_tenant.id)

    await _login(tenant_client, "jan@acme.cz", "contactpass")
    create_resp = await tenant_client.post(
        "/app/orders", data={"title": "S interním komentem"}, follow_redirects=False
    )
    order_id = UUID(create_resp.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0])

    await _logout(tenant_client)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")

    add_internal = await tenant_client.post(
        f"/app/orders/{order_id}/comments",
        data={"body": "POUZE PRO TYM", "is_internal": "1"},
        follow_redirects=False,
    )
    assert add_internal.status_code == 303

    add_public = await tenant_client.post(
        f"/app/orders/{order_id}/comments",
        data={"body": "verejny komentar"},
        follow_redirects=False,
    )
    assert add_public.status_code == 303

    # Staff sees both — with the resolved author name, not the generic
    # fallback label (F-BE-006: guards the batch author lookup).
    staff_detail = await tenant_client.get(f"/app/orders/{order_id}")
    assert "POUZE PRO TYM" in staff_detail.text
    assert "verejny komentar" in staff_detail.text
    assert "Staff User" in staff_detail.text

    # Contact sees only the public one, again with the real author name.
    await _logout(tenant_client)
    await _login(tenant_client, "jan@acme.cz", "contactpass")

    contact_detail = await tenant_client.get(f"/app/orders/{order_id}")
    assert "POUZE PRO TYM" not in contact_detail.text
    assert "verejny komentar" in contact_detail.text
    assert "Staff User" in contact_detail.text

    # Contact cannot mark a comment internal.
    bad = await tenant_client.post(
        f"/app/orders/{order_id}/comments",
        data={"body": "try", "is_internal": "1"},
        follow_redirects=False,
    )
    assert bad.status_code == 403


async def test_staff_can_cancel_order_with_reason(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed_everyone(owner_engine, demo_tenant.id)

    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    from sqlalchemy.ext.asyncio import async_sessionmaker

    # Staff creates order directly.
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        customer = (
            await session.execute(select(Customer).where(Customer.name == "ACME"))
        ).scalar_one()

    create_resp = await tenant_client.post(
        "/app/orders",
        data={"title": "Bude zrušena", "customer_id": str(customer.id)},
        follow_redirects=False,
    )
    assert create_resp.status_code == 303
    order_id = UUID(create_resp.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0])

    cancel = await tenant_client.post(
        f"/app/orders/{order_id}/transitions/cancelled", follow_redirects=False
    )
    assert cancel.status_code == 303

    order = await _order_row(owner_engine, order_id)
    assert order.status == OrderStatus.CANCELLED
    assert order.cancelled_at is not None


async def test_unquoted_order_shows_running_sum_of_priced_items(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """Before quoting, ``quoted_total`` is NULL by design (F-13), but the
    summary card read "—" next to three priced lines. It now shows the
    running sum, labelled as not yet quoted."""
    await _seed_everyone(owner_engine, demo_tenant.id)
    await _login(tenant_client, "jan@acme.cz", "contactpass")
    resp = await tenant_client.post(
        "/app/orders", data={"title": "Průběžný součet", "notes": ""}, follow_redirects=False
    )
    order_id = UUID(resp.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0])

    page = await tenant_client.get(f"/app/orders/{order_id}")
    assert re.search(r"data-running-total>\s*—", page.text)

    for qty, price in (("10", "125.50"), ("2", "100")):
        await tenant_client.post(
            f"/app/orders/{order_id}/items",
            data={"description": "Díl", "quantity": qty, "unit": "ks", "unit_price": price},
            follow_redirects=False,
        )
    # add_item keeps quoted_total current (F-14), so the card already shows
    # the sum. Orders seeded directly or written before that fix carry NULL
    # with priced lines — that is the case the running sum covers.
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE orders SET quoted_total = NULL WHERE id = :id"), {"id": order_id}
        )
    page = await tenant_client.get(f"/app/orders/{order_id}")
    running = re.search(r"data-running-total>\s*([^<]+?)\s*<", page.text)
    assert running is not None
    assert running.group(1).replace("\xa0", " ").startswith("1 455")


async def test_items_get_increasing_positions(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """``int(max_pos or -1) + 1`` gave every line position 0 (a first line
    at 0 is falsy), so ordering by position alone was random — this test's
    sibling flaked with the two prices swapped (2 700 instead of 1 800)."""
    await _seed_everyone(owner_engine, demo_tenant.id)
    await _login(tenant_client, "jan@acme.cz", "contactpass")
    resp = await tenant_client.post(
        "/app/orders", data={"title": "Pozice", "notes": ""}, follow_redirects=False
    )
    order_id = UUID(resp.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0])
    for name in ("A", "B", "C"):
        await tenant_client.post(
            f"/app/orders/{order_id}/items",
            data={"description": name, "quantity": "1", "unit": "ks"},
            follow_redirects=False,
        )
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        rows = (
            await session.execute(
                select(OrderItem.description, OrderItem.position)
                .where(OrderItem.order_id == order_id)
                .order_by(OrderItem.position)
            )
        ).all()
    assert [(r.description, r.position) for r in rows] == [("A", 0), ("B", 1), ("C", 2)]
