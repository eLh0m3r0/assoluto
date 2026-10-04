"""Art. 15 / 20 exports cover everything a principal did (SEC-5 / F-27).

Before: the staff export had no comments, status changes, uploads or
assigned orders; the contact export had only comments — no orders they
created, uploads, status changes, audit events or preferences.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.attachment import OrderAttachment
from app.models.audit_event import AuditEvent
from app.models.customer import Customer, CustomerContact
from app.models.enums import AttachmentKind, CustomerContactRole, OrderStatus, UserRole
from app.models.order import Order, OrderComment, OrderStatusHistory
from app.models.user import User
from app.services.gdpr_service import export_for_contact, export_for_user

pytestmark = pytest.mark.postgres


async def test_staff_and_contact_exports_are_complete(owner_engine, demo_tenant) -> None:
    tid = demo_tenant.id
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        user = User(
            id=uuid4(),
            tenant_id=tid,
            email="staff@4mex.cz",
            full_name="Staff",
            role=UserRole.TENANT_STAFF,
            notification_prefs={"scope": "involved", "_gdpr_erased_at": "x"},
            totp_secret="SECRETSEED",
        )
        customer = Customer(id=uuid4(), tenant_id=tid, name="ACME")
        session.add_all([user, customer])
        await session.flush()
        contact = CustomerContact(
            id=uuid4(),
            tenant_id=tid,
            customer_id=customer.id,
            email="jan@acme.cz",
            full_name="Jan",
            role=CustomerContactRole.CUSTOMER_ADMIN,
            notification_prefs={"scope": "all"},
            invited_at=datetime.now(UTC),
            accepted_at=datetime.now(UTC),
        )
        session.add(contact)
        await session.flush()
        order = Order(
            id=uuid4(),
            tenant_id=tid,
            customer_id=customer.id,
            number="2026-GDPR-1",
            title="Mine",
            status=OrderStatus.SUBMITTED,
            created_by_contact_id=contact.id,
            assigned_to_user_id=user.id,
        )
        session.add(order)
        await session.flush()
        session.add_all(
            [
                OrderComment(
                    id=uuid4(),
                    tenant_id=tid,
                    order_id=order.id,
                    author_contact_id=contact.id,
                    body="from Jan",
                ),
                OrderComment(
                    id=uuid4(),
                    tenant_id=tid,
                    order_id=order.id,
                    author_user_id=user.id,
                    body="from staff",
                ),
                OrderStatusHistory(
                    id=uuid4(),
                    tenant_id=tid,
                    order_id=order.id,
                    from_status=OrderStatus.DRAFT,
                    to_status=OrderStatus.SUBMITTED,
                    changed_by_contact_id=contact.id,
                ),
                OrderStatusHistory(
                    id=uuid4(),
                    tenant_id=tid,
                    order_id=order.id,
                    from_status=OrderStatus.SUBMITTED,
                    to_status=OrderStatus.QUOTED,
                    changed_by_user_id=user.id,
                ),
                OrderAttachment(
                    id=uuid4(),
                    tenant_id=tid,
                    order_id=order.id,
                    kind=AttachmentKind.DRAWING,
                    filename="jan.pdf",
                    content_type="application/pdf",
                    size_bytes=10,
                    storage_key="k/jan.pdf",
                    uploaded_by_contact_id=contact.id,
                ),
                OrderAttachment(
                    id=uuid4(),
                    tenant_id=tid,
                    order_id=order.id,
                    kind=AttachmentKind.DRAWING,
                    filename="staff.pdf",
                    content_type="application/pdf",
                    size_bytes=10,
                    storage_key="k/staff.pdf",
                    uploaded_by_user_id=user.id,
                ),
                AuditEvent(
                    tenant_id=tid,
                    occurred_at=datetime.now(UTC),
                    actor_type="contact",
                    actor_id=contact.id,
                    actor_label="Jan",
                    action="order.comment_added",
                    entity_type="order",
                    entity_id=order.id,
                    entity_label=order.number,
                ),
            ]
        )

    async with sm() as session:
        c = await export_for_contact(session, contact=contact)
        u = await export_for_user(session, user=user)

    assert [o["number"] for o in c["orders_created"]] == ["2026-GDPR-1"]
    assert [x["body"] for x in c["comments_authored"]] == ["from Jan"]
    assert [x["to_status"] for x in c["status_changes"]] == ["submitted"]
    assert [x["filename"] for x in c["attachments_uploaded"]] == ["jan.pdf"]
    assert [x["action"] for x in c["audit_events_authored"]] == ["order.comment_added"]
    assert c["profile"]["notification_prefs"] == {"scope": "all"}

    assert [o["number"] for o in u["orders_assigned"]] == ["2026-GDPR-1"]
    assert [x["body"] for x in u["comments_authored"]] == ["from staff"]
    assert [x["to_status"] for x in u["status_changes"]] == ["quoted"]
    assert [x["filename"] for x in u["attachments_uploaded"]] == ["staff.pdf"]
    # Preferences exported, machine markers and secrets not.
    assert u["profile"]["notification_prefs"] == {"scope": "involved"}
    assert u["profile"]["two_factor_enabled"] is True
    assert "SECRETSEED" not in str(u)
