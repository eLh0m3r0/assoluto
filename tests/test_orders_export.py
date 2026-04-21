"""CSV export (``GET /app/orders.csv``) — scoping, header, filters.

Mirrors the ``tests/test_orders_filters.py`` seeding pattern so the two
suites exercise the same shared ``build_orders_query`` plumbing.
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.customer import Customer, CustomerContact
from app.models.enums import CustomerContactRole, OrderStatus, UserRole
from app.models.order import Order, OrderStatusHistory
from app.models.user import User
from app.security.passwords import hash_password

pytestmark = pytest.mark.postgres


async def _seed(owner_engine, tenant_id) -> dict:
    """Create staff + 2 customers + 1 contact on customer A + a handful of
    orders split across customers and statuses."""
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
        await session.flush()

        orders = []
        # 6 orders: 3 on ACME (2 DRAFT, 1 QUOTED) + 3 on Other (2 DRAFT, 1 QUOTED).
        specs = [
            (acme, OrderStatus.DRAFT, "ACME draft 1"),
            (acme, OrderStatus.DRAFT, "ACME draft 2"),
            (acme, OrderStatus.QUOTED, "ACME quoted"),
            (other, OrderStatus.DRAFT, "Other draft 1"),
            (other, OrderStatus.DRAFT, "Other draft 2"),
            (other, OrderStatus.QUOTED, "Other quoted"),
        ]
        for i, (cust, status, title) in enumerate(specs):
            order = Order(
                id=uuid4(),
                tenant_id=tenant_id,
                customer_id=cust.id,
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


async def test_csv_has_bom_and_expected_header(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "owner@4mex.cz", "ownerpass")

    resp = await tenant_client.get("/app/orders.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    assert ".csv" in resp.headers["content-disposition"]

    body = resp.text
    # UTF-8 BOM is the very first character so Excel (CZ locale) opens the
    # file in the correct encoding.
    assert body.startswith("﻿"), f"missing BOM; got leading bytes: {body[:8]!r}"

    # Header row — semicolon-delimited, first 9 columns.
    header = body.lstrip("﻿").split("\r\n", 1)[0]
    cols = header.split(";")
    assert len(cols) == 9
    # The English defaults appear when Accept-Language does not negotiate
    # a CZ catalog entry; either way the column order is deterministic.
    assert cols[0] in ("Order number", "Číslo zakázky")
    # Last columns cover currency + item_count.
    assert cols[-1] in ("Items", "Položek", "Počet položek")


async def test_staff_sees_all_tenant_orders(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "owner@4mex.cz", "ownerpass")

    resp = await tenant_client.get("/app/orders.csv")
    assert resp.status_code == 200
    body = resp.text
    # 1 header + 6 data rows (seeded above).
    data_lines = [ln for ln in body.splitlines() if ln.strip()]
    assert len(data_lines) == 7  # header + 6 orders

    # Each customer name appears in the rendered rows.
    assert "ACME" in body
    assert "Other" in body


async def test_contact_sees_only_own_customer_orders(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "jan@acme.cz", "contactpass")

    resp = await tenant_client.get("/app/orders.csv")
    assert resp.status_code == 200
    body = resp.text

    # 1 header + 3 data rows for ACME only.
    data_lines = [ln for ln in body.splitlines() if ln.strip()]
    assert len(data_lines) == 4

    # Contact MUST NOT see the Other customer's rows — scoping check.
    assert "Other draft" not in body
    assert "Other quoted" not in body
    assert "ACME" in body


async def test_status_filter_narrows_rows(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "owner@4mex.cz", "ownerpass")

    resp = await tenant_client.get("/app/orders.csv?status=quoted")
    assert resp.status_code == 200
    body = resp.text
    data_lines = [ln for ln in body.splitlines() if ln.strip()]
    # 1 header + 2 quoted orders (one per customer).
    assert len(data_lines) == 3
    assert "quoted" in body
    # Drafts must not leak through the filter.
    assert "ACME draft" not in body
    assert "Other draft" not in body
