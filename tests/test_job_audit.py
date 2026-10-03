"""Scheduled jobs that mutate data leave an audit trail (BE-13 / F-34)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.audit_event import AuditEvent
from app.models.customer import CustomerContact
from app.models.enums import CustomerContactRole, OrderStatus
from app.models.order import Order
from tests.test_notifications_flow import _seed

pytestmark = pytest.mark.postgres


async def _events(owner_engine, action: str) -> list[AuditEvent]:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        return list(
            (await session.execute(select(AuditEvent).where(AuditEvent.action == action)))
            .scalars()
            .all()
        )


async def test_auto_close_records_a_system_status_change(owner_engine, demo_tenant) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    order_id = uuid4()
    async with sm() as session, session.begin():
        session.add(
            Order(
                id=order_id,
                tenant_id=demo_tenant.id,
                customer_id=seed["customer"].id,
                number="2026-AUD-1",
                title="Old delivery",
                status=OrderStatus.DELIVERED,
                created_by_user_id=seed["staff"].id,
            )
        )
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE orders SET updated_at = :ts WHERE id = :id"),
            {"ts": datetime.now(UTC) - timedelta(days=20), "id": order_id},
        )

    from app.tasks.periodic import auto_close_delivered_orders

    assert await auto_close_delivered_orders() == 1

    events = await _events(owner_engine, "order.status_changed")
    assert len(events) == 1
    ev = events[0]
    assert ev.tenant_id == demo_tenant.id
    assert ev.entity_id == order_id
    assert ev.actor_type == "system"
    assert ev.actor_id is None
    assert "auto_close_delivered_orders" in ev.actor_label
    assert ev.diff["before"] == {"status": "delivered"}
    assert ev.diff["after"]["status"] == "closed"


async def test_invite_purge_records_one_event_per_contact(owner_engine, demo_tenant) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    contact_id = uuid4()
    async with sm() as session, session.begin():
        session.add(
            CustomerContact(
                id=contact_id,
                tenant_id=demo_tenant.id,
                customer_id=seed["customer"].id,
                email="gone@acme.cz",
                full_name="Gone",
                role=CustomerContactRole.CUSTOMER_USER,
                invited_at=datetime.now(UTC) - timedelta(days=30),
            )
        )

    from app.tasks.periodic import cleanup_stale_invited_contacts

    assert await cleanup_stale_invited_contacts() == 1

    events = await _events(owner_engine, "contact.invite_expired")
    assert [e.entity_id for e in events] == [contact_id]
    assert events[0].actor_type == "system"
    assert "cleanup_stale_invited_contacts" in events[0].actor_label
    assert events[0].entity_label == "gone@acme.cz"
    assert events[0].tenant_id == demo_tenant.id


async def test_resending_an_invitation_restarts_the_purge_clock(
    tenant_client, owner_engine, demo_tenant
) -> None:
    """Audit LOGIC-10: a link re-sent on day 13 must not be purged on day 14."""
    from app.email.sender import CaptureSender
    from app.security.email_throttle import INVITE_RESEND_THROTTLE
    from tests.test_notifications_flow import _login

    INVITE_RESEND_THROTTLE.reset()
    seed = await _seed(owner_engine, demo_tenant.id)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    contact_id = uuid4()
    async with sm() as session, session.begin():
        session.add(
            CustomerContact(
                id=contact_id,
                tenant_id=demo_tenant.id,
                customer_id=seed["customer"].id,
                email="late@acme.cz",
                full_name="Late",
                role=CustomerContactRole.CUSTOMER_USER,
                invited_at=datetime.now(UTC) - timedelta(days=13),
            )
        )

    tenant_client._transport.app.state.email_sender = CaptureSender()  # type: ignore[attr-defined]
    await _login(tenant_client, "owner@4mex.cz", "staffpass")
    resp = await tenant_client.post(
        f"/app/customers/{seed['customer'].id}/contacts/{contact_id}/resend-invite",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "notice=" in resp.headers["location"]

    from app.tasks.periodic import cleanup_stale_invited_contacts

    # Two days later (day 15 of the original invite) the contact survives.
    assert await cleanup_stale_invited_contacts(now=datetime.now(UTC) + timedelta(days=2)) == 0
    async with sm() as session:
        still_there = (
            await session.execute(select(CustomerContact).where(CustomerContact.id == contact_id))
        ).scalar_one_or_none()
    assert still_there is not None
