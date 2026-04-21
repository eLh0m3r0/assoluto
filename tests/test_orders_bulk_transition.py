"""Bulk status transitions on the orders list (§3 of ultraplan).

These tests drive the real app through ``POST /app/orders/bulk/transition``
to ensure that:

* Staff can transition a batch of orders in one request, and every
  order picks up ``transition_order``'s side effects (status change,
  ``OrderStatusHistory`` row, timestamp setters).
* Partial failures (mix of valid + already-in-target statuses) do not
  roll back the successful ones — ``BulkResult`` surfaces the failures.
* Customer contacts are forbidden (403) — the route uses
  ``require_tenant_staff``.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.customer import Customer, CustomerContact
from app.models.enums import CustomerContactRole, OrderStatus, UserRole
from app.models.order import Order, OrderStatusHistory
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

        return {"staff": staff, "customer": customer, "contact": contact}


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


async def _create_draft_order(client: AsyncClient, title: str) -> UUID:
    """Contact-authored draft order; returns the new order's UUID."""
    resp = await client.post(
        "/app/orders",
        data={"title": title},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    return UUID(resp.headers["location"].rsplit("/", 1)[-1])


async def _orders_by_ids(owner_engine, ids: list[UUID]) -> list[Order]:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        result = await session.execute(select(Order).where(Order.id.in_(ids)))
        return list(result.scalars().all())


async def _status_history_count(owner_engine, order_id: UUID) -> int:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        rows = (
            (
                await session.execute(
                    select(OrderStatusHistory).where(OrderStatusHistory.order_id == order_id)
                )
            )
            .scalars()
            .all()
        )
        return len(list(rows))


# ---------------------------------------------------------------------- tests


async def test_bulk_transition_three_drafts_to_submitted(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """Happy path: 3 DRAFT orders → SUBMITTED via one bulk POST."""
    await _seed_everyone(owner_engine, demo_tenant.id)

    # Contact creates three drafts (simplest way to get orders on the tenant).
    await _login(tenant_client, "jan@acme.cz", "contactpass")
    ids = [await _create_draft_order(tenant_client, f"Bulk draft {i}") for i in range(3)]
    await _logout(tenant_client)

    # Staff submits all three in one request.
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    resp = await tenant_client.post(
        "/app/orders/bulk/transition",
        data={"order_ids": [str(i) for i in ids], "to_status": "submitted"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"].startswith("/app/orders?notice=")

    orders = await _orders_by_ids(owner_engine, ids)
    assert len(orders) == 3
    for o in orders:
        assert o.status == OrderStatus.SUBMITTED
        assert o.submitted_at is not None
        # One history row for DRAFT creation + one for SUBMITTED transition.
        assert await _status_history_count(owner_engine, o.id) == 2


async def test_bulk_transition_partial_failure(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """Orders already in the target status fail; the rest still commit."""
    await _seed_everyone(owner_engine, demo_tenant.id)

    await _login(tenant_client, "jan@acme.cz", "contactpass")
    ids = [await _create_draft_order(tenant_client, f"Partial {i}") for i in range(3)]
    await _logout(tenant_client)

    await _login(tenant_client, "staff@4mex.cz", "staffpass")

    # Move the first order to SUBMITTED so re-running a bulk SUBMITTED
    # request hits the "already in that status" guard for that one while
    # the other two still transition cleanly.
    single = await tenant_client.post(
        f"/app/orders/{ids[0]}/transitions/submitted", follow_redirects=False
    )
    assert single.status_code == 303

    resp = await tenant_client.post(
        "/app/orders/bulk/transition",
        data={"order_ids": [str(i) for i in ids], "to_status": "submitted"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # Flash summary mentions both a success and a failure count.
    assert "notice=" in resp.headers["location"]

    orders = await _orders_by_ids(owner_engine, ids)
    by_id = {o.id: o for o in orders}
    # All three land in SUBMITTED — the first was already there, the
    # other two transitioned in this batch.
    for oid in ids:
        assert by_id[oid].status == OrderStatus.SUBMITTED

    # The two orders that actually transitioned in the bulk call now
    # have a second history row (DRAFT create + SUBMITTED).
    assert await _status_history_count(owner_engine, ids[1]) == 2
    assert await _status_history_count(owner_engine, ids[2]) == 2
    # The first order transitioned earlier (single-order route) and was
    # skipped by the bulk call — it has exactly 2 history rows, not 3.
    assert await _status_history_count(owner_engine, ids[0]) == 2


async def test_bulk_transition_forbidden_for_contact(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """Customer contacts get 403 — the route is staff-only."""
    await _seed_everyone(owner_engine, demo_tenant.id)

    await _login(tenant_client, "jan@acme.cz", "contactpass")
    order_id = await _create_draft_order(tenant_client, "No bulk for contacts")

    resp = await tenant_client.post(
        "/app/orders/bulk/transition",
        data={"order_ids": [str(order_id)], "to_status": "submitted"},
        follow_redirects=False,
    )
    assert resp.status_code == 403

    orders = await _orders_by_ids(owner_engine, [order_id])
    assert orders[0].status == OrderStatus.DRAFT
