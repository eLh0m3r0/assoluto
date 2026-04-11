"""Tests for order notifications and the periodic auto-close job."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from freezegun import freeze_time
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.email.sender import CaptureSender
from app.models.customer import Customer, CustomerContact
from app.models.enums import CustomerContactRole, OrderStatus, UserRole
from app.models.order import Order
from app.models.user import User
from app.security.passwords import hash_password

pytestmark = pytest.mark.postgres


async def _seed(owner_engine, tenant_id: UUID) -> dict:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        staff = User(
            id=uuid4(),
            tenant_id=tenant_id,
            email="owner@4mex.cz",
            full_name="Owner",
            role=UserRole.TENANT_ADMIN,
            password_hash=hash_password("staffpass"),
        )
        customer = Customer(id=uuid4(), tenant_id=tenant_id, name="ACME", ico="11111111")
        session.add_all([staff, customer])
        await session.flush()
        contact = CustomerContact(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=customer.id,
            email="jan@acme.cz",
            full_name="Jan",
            role=CustomerContactRole.CUSTOMER_ADMIN,
            password_hash=hash_password("janpass"),
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


def _capture(client: AsyncClient) -> CaptureSender:
    capture = CaptureSender()
    client._transport.app.state.email_sender = capture  # type: ignore[attr-defined]
    return capture


async def test_submitting_order_emails_tenant_admin(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    capture = _capture(tenant_client)

    # Contact creates + submits.
    await _login(tenant_client, "jan@acme.cz", "janpass")
    create_resp = await tenant_client.post(
        "/app/orders", data={"title": "Nová zakázka"}, follow_redirects=False
    )
    order_id = UUID(create_resp.headers["location"].rsplit("/", 1)[-1])
    await tenant_client.post(
        f"/app/orders/{order_id}/items",
        data={"description": "Řezání", "quantity": "5", "unit": "ks"},
        follow_redirects=False,
    )
    submit = await tenant_client.post(
        f"/app/orders/{order_id}/transitions/submitted", follow_redirects=False
    )
    assert submit.status_code == 303

    # One email to the tenant admin.
    assert len(capture.outbox) == 1
    msg = capture.outbox[0]
    assert msg.to == "owner@4mex.cz"
    assert "Nová objednávka" in msg.subject
    assert "ACME" in msg.subject
    assert "Nová zakázka" in msg.html
    assert f"/app/orders/{order_id}" in msg.html


async def test_staff_public_comment_emails_contact(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)

    await _login(tenant_client, "jan@acme.cz", "janpass")
    create_resp = await tenant_client.post(
        "/app/orders", data={"title": "Notif test"}, follow_redirects=False
    )
    order_id = UUID(create_resp.headers["location"].rsplit("/", 1)[-1])

    # Swap to staff, install capture, add a public comment.
    await tenant_client.post("/auth/logout", follow_redirects=False)
    tenant_client.cookies.clear()
    await _login(tenant_client, "owner@4mex.cz", "staffpass")
    capture = _capture(tenant_client)

    resp = await tenant_client.post(
        f"/app/orders/{order_id}/comments",
        data={"body": "Zdravim, podivam se na to"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    assert len(capture.outbox) == 1
    msg = capture.outbox[0]
    assert msg.to == "jan@acme.cz"
    assert "Nový komentář" in msg.subject
    assert "Zdravim, podivam se na to" in msg.html


async def test_internal_comment_does_not_trigger_notification(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "jan@acme.cz", "janpass")
    create_resp = await tenant_client.post(
        "/app/orders", data={"title": "Silent comment"}, follow_redirects=False
    )
    order_id = UUID(create_resp.headers["location"].rsplit("/", 1)[-1])

    await tenant_client.post("/auth/logout", follow_redirects=False)
    tenant_client.cookies.clear()
    await _login(tenant_client, "owner@4mex.cz", "staffpass")
    capture = _capture(tenant_client)

    resp = await tenant_client.post(
        f"/app/orders/{order_id}/comments",
        data={"body": "only for the team", "is_internal": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert capture.outbox == []


async def test_contact_comment_emails_tenant_admins(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    # Contact creates order + leaves a comment; staff gets an email.
    capture = _capture(tenant_client)

    await _login(tenant_client, "jan@acme.cz", "janpass")
    create_resp = await tenant_client.post(
        "/app/orders", data={"title": "Contact commented"}, follow_redirects=False
    )
    order_id = UUID(create_resp.headers["location"].rsplit("/", 1)[-1])

    resp = await tenant_client.post(
        f"/app/orders/{order_id}/comments",
        data={"body": "otazka ke specifikaci"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    # Inbox: one email to owner@4mex.cz.
    admin_hits = [m for m in capture.outbox if m.to == "owner@4mex.cz"]
    assert len(admin_hits) == 1
    assert "Nový komentář" in admin_hits[0].subject


async def test_staff_quoting_emails_customer_contacts(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)

    await _login(tenant_client, "jan@acme.cz", "janpass")
    create_resp = await tenant_client.post(
        "/app/orders", data={"title": "Se cenou"}, follow_redirects=False
    )
    order_id = UUID(create_resp.headers["location"].rsplit("/", 1)[-1])
    await tenant_client.post(
        f"/app/orders/{order_id}/items",
        data={"description": "A", "quantity": "1", "unit": "ks"},
        follow_redirects=False,
    )
    await tenant_client.post(
        f"/app/orders/{order_id}/transitions/submitted", follow_redirects=False
    )

    # Switch to staff, install capture AFTER submit so only the status-
    # change email from the upcoming transition is recorded.
    await _logout(tenant_client)
    await _login(tenant_client, "owner@4mex.cz", "staffpass")
    capture = _capture(tenant_client)

    # Give the priced line a price then transition to QUOTED.
    await tenant_client.post(
        f"/app/orders/{order_id}/items",
        data={"description": "Doprava", "quantity": "1", "unit": "ks", "unit_price": "500"},
        follow_redirects=False,
    )
    quoted = await tenant_client.post(
        f"/app/orders/{order_id}/transitions/quoted", follow_redirects=False
    )
    assert quoted.status_code == 303

    assert len(capture.outbox) == 1
    msg = capture.outbox[0]
    assert msg.to == "jan@acme.cz"
    assert "Nacenění" in msg.subject
    assert "Se cenou" in msg.html


@pytest.mark.postgres
async def test_auto_close_delivered_orders_after_14_days(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)

    # Directly seed one DELIVERED order whose updated_at is 15 days old.
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        old_updated = datetime.now(UTC) - timedelta(days=15)
        order = Order(
            id=uuid4(),
            tenant_id=demo_tenant.id,
            customer_id=seed["customer"].id,
            number="2026-999001",
            title="Stale delivered",
            status=OrderStatus.DELIVERED,
            created_by_user_id=seed["staff"].id,
        )
        session.add(order)
        await session.flush()

        # Force updated_at back — the server default is now(), so UPDATE it.
        await session.execute(
            select(Order).where(Order.id == order.id).execution_options(synchronize_session=False)
        )
        from sqlalchemy import text

        await session.execute(
            text("UPDATE orders SET updated_at = :ts WHERE id = :id"),
            {"ts": old_updated, "id": order.id},
        )

    # Run the periodic task directly (same code path as APScheduler).
    from app.tasks.periodic import auto_close_delivered_orders

    with freeze_time(datetime.now(UTC)):
        closed = await auto_close_delivered_orders()
    assert closed == 1

    async with sm() as session:
        refreshed = (
            await session.execute(select(Order).where(Order.number == "2026-999001"))
        ).scalar_one()
    assert refreshed.status == OrderStatus.CLOSED
    assert refreshed.closed_at is not None


@pytest.mark.postgres
async def test_cleanup_stale_invited_contacts_removes_old_pending(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """Invited-but-never-accepted contacts older than 14 days get purged."""
    from sqlalchemy import select as _select
    from sqlalchemy import text as _text

    seed = await _seed(owner_engine, demo_tenant.id)

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    old_ts = datetime.now(UTC) - timedelta(days=20)
    fresh_ts = datetime.now(UTC)

    async with sm() as session, session.begin():
        session.add_all(
            [
                CustomerContact(
                    id=uuid4(),
                    tenant_id=demo_tenant.id,
                    customer_id=seed["customer"].id,
                    email="stale@acme.cz",
                    full_name="Stale",
                    role=CustomerContactRole.CUSTOMER_USER,
                    password_hash=None,
                    invited_at=fresh_ts,
                    accepted_at=None,
                ),
                CustomerContact(
                    id=uuid4(),
                    tenant_id=demo_tenant.id,
                    customer_id=seed["customer"].id,
                    email="recent@acme.cz",
                    full_name="Recent",
                    role=CustomerContactRole.CUSTOMER_USER,
                    password_hash=None,
                    invited_at=fresh_ts,
                    accepted_at=None,
                ),
            ]
        )
        await session.flush()

    # Backdate in a SEPARATE transaction so the ORM session's in-memory
    # state can't race the raw UPDATE.
    async with owner_engine.begin() as conn:
        await conn.execute(
            _text("UPDATE customer_contacts SET invited_at = :ts WHERE email = 'stale@acme.cz'"),
            {"ts": old_ts},
        )

    from app.tasks.periodic import cleanup_stale_invited_contacts

    removed = await cleanup_stale_invited_contacts()
    assert removed == 1

    async with sm() as session:
        emails = (
            (
                await session.execute(
                    _select(CustomerContact.email).where(
                        CustomerContact.tenant_id == demo_tenant.id
                    )
                )
            )
            .scalars()
            .all()
        )
    # "jan@acme.cz" (accepted) + "recent@acme.cz" remain; "stale@acme.cz" gone.
    assert "stale@acme.cz" not in emails
    assert "recent@acme.cz" in emails


@pytest.mark.postgres
async def test_cleanup_leaves_accepted_contacts_alone(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """Old but accepted contacts are never touched."""
    from sqlalchemy import text as _text

    seed = await _seed(owner_engine, demo_tenant.id)

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        session.add(
            CustomerContact(
                id=uuid4(),
                tenant_id=demo_tenant.id,
                customer_id=seed["customer"].id,
                email="veteran@acme.cz",
                full_name="Veteran",
                role=CustomerContactRole.CUSTOMER_USER,
                password_hash="fake",
                invited_at=datetime.now(UTC),
                accepted_at=datetime.now(UTC),
            )
        )

    async with owner_engine.begin() as conn:
        await conn.execute(
            _text("UPDATE customer_contacts SET invited_at = :ts WHERE email = 'veteran@acme.cz'"),
            {"ts": datetime.now(UTC) - timedelta(days=60)},
        )

    from app.tasks.periodic import cleanup_stale_invited_contacts

    removed = await cleanup_stale_invited_contacts()
    assert removed == 0


@pytest.mark.postgres
async def test_auto_close_does_not_touch_recent_orders(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        session.add(
            Order(
                id=uuid4(),
                tenant_id=demo_tenant.id,
                customer_id=seed["customer"].id,
                number="2026-999002",
                title="Recent delivered",
                status=OrderStatus.DELIVERED,
                created_by_user_id=seed["staff"].id,
            )
        )

    from app.tasks.periodic import auto_close_delivered_orders

    closed = await auto_close_delivered_orders()
    assert closed == 0
