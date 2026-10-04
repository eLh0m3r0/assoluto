"""Weekly open-orders summary to opted-in customers (IDEA-10)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.email.sender import CaptureSender
from app.models.customer import Customer, CustomerContact
from app.models.enums import CustomerContactRole, OrderStatus, UserRole
from app.models.order import Order
from app.models.user import User
from app.security.passwords import hash_password
from app.tasks.periodic import send_weekly_order_summaries

pytestmark = pytest.mark.postgres

MONDAY = datetime(2026, 10, 5, 5, 0, tzinfo=UTC)


async def _seed(owner_engine, tenant_id, *, enabled=True, admin_prefs=None) -> Customer:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    now = datetime.now(UTC)
    async with sm() as session, session.begin():
        customer = Customer(
            id=uuid4(),
            tenant_id=tenant_id,
            name="Strojírna Ukázková s.r.o.",
            weekly_summary_enabled=enabled,
        )
        session.add(customer)
        await session.flush()
        session.add_all(
            [
                CustomerContact(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    customer_id=customer.id,
                    email="admin@ukazkova.cz",
                    full_name="Admin",
                    role=CustomerContactRole.CUSTOMER_ADMIN,
                    password_hash=hash_password("x" * 10),
                    invited_at=now,
                    accepted_at=now,
                    notification_prefs=admin_prefs or {},
                ),
                CustomerContact(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    customer_id=customer.id,
                    email="user@ukazkova.cz",
                    full_name="User",
                    role=CustomerContactRole.CUSTOMER_USER,
                    password_hash=hash_password("x" * 10),
                    invited_at=now,
                    accepted_at=now,
                ),
            ]
        )
        for i, status in enumerate(
            [
                OrderStatus.DRAFT,
                OrderStatus.SUBMITTED,
                OrderStatus.IN_PRODUCTION,
                OrderStatus.DELIVERED,
                OrderStatus.CANCELLED,
            ]
        ):
            session.add(
                Order(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    customer_id=customer.id,
                    number=f"2026-{i + 1:06d}",
                    title=f"Zakázka {status.value}",
                    status=status,
                    promised_delivery_at=date(2026, 10, 20),
                )
            )
    return customer


async def test_not_opted_in_sends_nothing(owner_engine, demo_tenant, settings) -> None:
    await _seed(owner_engine, demo_tenant.id, enabled=False)
    capture = CaptureSender()
    assert await send_weekly_order_summaries(now=MONDAY, sender=capture) == 0
    assert capture.outbox == []


async def test_admin_gets_open_orders_once_per_week(owner_engine, demo_tenant, settings) -> None:
    settings.platform_cookie_domain = ".assoluto.test"
    await _seed(owner_engine, demo_tenant.id)
    capture = CaptureSender()

    assert await send_weekly_order_summaries(now=MONDAY, sender=capture) == 1
    mail = capture.outbox[0]
    assert mail.to == "admin@ukazkova.cz"
    assert "Zakázka submitted" in mail.text
    assert "Zakázka in_production" in mail.text
    assert "20.10.2026" in mail.text
    for not_open in ("Zakázka draft", "Zakázka delivered", "Zakázka cancelled"):
        assert not_open not in mail.text
    assert "ref=portal" in mail.text  # contact mail carries the footer

    # Same ISO week: nothing again.
    assert await send_weekly_order_summaries(now=MONDAY + timedelta(hours=3), sender=capture) == 0
    # Next week: again.
    assert await send_weekly_order_summaries(now=MONDAY + timedelta(days=7), sender=capture) == 1


async def test_admin_opt_out_is_respected_and_audience_yields(
    owner_engine, demo_tenant, settings
) -> None:
    await _seed(
        owner_engine,
        demo_tenant.id,
        admin_prefs={"events": {"weekly_summary": False}},
    )
    capture = CaptureSender()
    assert await send_weekly_order_summaries(now=MONDAY, sender=capture) == 1
    # The admin said no — never re-added; relevance yields to the colleague.
    assert [m.to for m in capture.outbox] == ["user@ukazkova.cz"]


async def test_everyone_opted_out_means_no_mail(owner_engine, demo_tenant, settings) -> None:
    await _seed(owner_engine, demo_tenant.id, admin_prefs={"events": {"weekly_summary": False}})
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE customer_contacts SET notification_prefs = "
                "'{\"events\": {\"weekly_summary\": false}}' WHERE email = 'user@ukazkova.cz'"
            )
        )
    capture = CaptureSender()
    assert await send_weekly_order_summaries(now=MONDAY, sender=capture) == 0


async def test_archived_or_idle_customer_gets_nothing(owner_engine, demo_tenant, settings) -> None:
    customer = await _seed(owner_engine, demo_tenant.id)
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE customers SET is_active = false WHERE id = :c"), {"c": customer.id}
        )
    capture = CaptureSender()
    assert await send_weekly_order_summaries(now=MONDAY, sender=capture) == 0

    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE customers SET is_active = true WHERE id = :c"), {"c": customer.id}
        )
        await conn.execute(
            text("UPDATE orders SET status = 'CLOSED' WHERE customer_id = :c"), {"c": customer.id}
        )
    assert await send_weekly_order_summaries(now=MONDAY, sender=capture) == 0


async def test_staff_toggle_on_customer_edit_form(tenant_client, owner_engine, demo_tenant) -> None:
    customer = await _seed(owner_engine, demo_tenant.id, enabled=False)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        session.add(
            User(
                id=uuid4(),
                tenant_id=demo_tenant.id,
                email="owner@4mex.cz",
                full_name="Owner",
                role=UserRole.TENANT_ADMIN,
                password_hash=hash_password("ownerpass1"),
            )
        )
    resp = await tenant_client.post(
        "/auth/login",
        data={"email": "owner@4mex.cz", "password": "ownerpass1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    form = await tenant_client.get(f"/app/customers/{customer.id}/edit")
    assert 'name="weekly_summary_enabled"' in form.text
    resp = await tenant_client.post(
        f"/app/customers/{customer.id}",
        data={"name": customer.name, "weekly_summary_enabled": "on"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    async with owner_engine.connect() as conn:
        enabled = (
            await conn.execute(
                text("SELECT weekly_summary_enabled FROM customers WHERE id = :c"),
                {"c": customer.id},
            )
        ).scalar_one()
    assert enabled is True


def test_contact_prefs_offer_the_weekly_summary_switch() -> None:
    from app.services.notification_prefs import (
        CONTACT_EVENTS,
        STAFF_EVENTS,
        NotificationEvent,
    )

    assert NotificationEvent.WEEKLY_SUMMARY in CONTACT_EVENTS
    assert NotificationEvent.WEEKLY_SUMMARY not in STAFF_EVENTS
