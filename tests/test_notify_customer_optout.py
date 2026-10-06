""" "Notify the customer by e-mail" opt-out on staff status changes (LOGIC-22).

A sender-side choice per action: unticked, the customer side gets no
mail for that move; staff mail and the consent rules (CLAUDE.md §19)
are untouched, contacts' own actions are unaffected, and the history row
plus the audit event record that the customer was not told.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.email.sender import CaptureSender
from app.i18n import gettext
from app.models.audit_event import AuditEvent
from app.models.customer import Customer, CustomerContact
from app.models.enums import CustomerContactRole, OrderStatus, UserRole
from app.models.order import Order, OrderItem, OrderStatusHistory
from app.models.user import User
from app.security.passwords import hash_password

pytestmark = pytest.mark.postgres

NOT_NOTIFIED = "Customer not notified by e-mail."
#: The ticked checkbox as the browser sends it: hidden "0" + checkbox "1".
NOTIFY_ON = ["0", "1"]
#: Unticked: only the hidden field arrives.
NOTIFY_OFF = ["0"]


def _is_not_notified_marker(text: str | None) -> bool:
    return bool(text) and any(gettext(loc, NOT_NOTIFIED) in text for loc in ("cs", "en", "de"))


async def _seed(owner_engine, tenant_id: UUID, *, orders: int = 1, status=OrderStatus.SUBMITTED):
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        owner = User(
            id=uuid4(),
            tenant_id=tenant_id,
            email="owner@4mex.cz",
            full_name="Owner",
            role=UserRole.TENANT_ADMIN,
            password_hash=hash_password("staffpass"),
        )
        operator = User(
            id=uuid4(),
            tenant_id=tenant_id,
            email="operator@4mex.cz",
            full_name="Operator",
            role=UserRole.TENANT_STAFF,
            password_hash=hash_password("staffpass"),
        )
        customer = Customer(id=uuid4(), tenant_id=tenant_id, name="ACME", ico="11111111")
        session.add_all([owner, operator, customer])
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
        order_ids = []
        for i in range(orders):
            order = Order(
                id=uuid4(),
                tenant_id=tenant_id,
                customer_id=customer.id,
                number=f"2026-{i + 1:06d}",
                title=f"Zakázka {i + 1}",
                status=status,
                created_by_contact_id=contact.id,
            )
            session.add(order)
            await session.flush()
            session.add(
                OrderItem(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    order_id=order.id,
                    position=0,
                    description="Díl",
                    quantity=1,
                    unit="ks",
                    unit_price=100,
                    line_total=100,
                )
            )
            order_ids.append(order.id)
        await session.flush()
    return order_ids


async def _login(client: AsyncClient, email: str, password: str) -> None:
    resp = await client.post(
        "/auth/login", data={"email": email, "password": password}, follow_redirects=False
    )
    assert resp.status_code == 303, resp.text


def _capture(client: AsyncClient) -> CaptureSender:
    capture = CaptureSender()
    client._transport.app.state.email_sender = capture  # type: ignore[attr-defined]
    return capture


async def _latest_history(owner_engine, order_id: UUID) -> OrderStatusHistory:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        return (
            (
                await session.execute(
                    select(OrderStatusHistory)
                    .where(OrderStatusHistory.order_id == order_id)
                    .order_by(OrderStatusHistory.created_at.desc())
                )
            )
            .scalars()
            .first()
        )


async def _status_audit(owner_engine, order_id: UUID) -> dict:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.entity_id == order_id,
                        AuditEvent.action == "order.status_changed",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    return events[0].diff["after"]


# ------------------------------------------------------------------ UI


async def test_checkbox_rendered_and_checked_by_default(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    [order_id] = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "owner@4mex.cz", "staffpass")

    detail = (await tenant_client.get(f"/app/orders/{order_id}")).text
    form = detail[detail.index('action="/app/orders/' + str(order_id) + '/status"') :]
    form = form[: form.index("</form>")]
    assert '<input type="hidden" name="notify_customer" value="0">' in form
    assert '<input type="checkbox" name="notify_customer" value="1" checked>' in form

    listing = (await tenant_client.get("/app/orders")).text
    bulk = listing[listing.index('action="/app/orders/bulk/transition"') :]
    bulk = bulk[: bulk.index("</form>")]
    assert '<input type="hidden" name="notify_customer" value="0">' in bulk
    assert '<input type="checkbox" name="notify_customer" value="1" checked>' in bulk


# ---------------------------------------------------------- single order


async def test_ticked_checkbox_notifies_the_customer(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    [order_id] = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "owner@4mex.cz", "staffpass")
    capture = _capture(tenant_client)

    resp = await tenant_client.post(
        f"/app/orders/{order_id}/status",
        data={"to_status": "in_production", "notify_customer": NOTIFY_ON},
        follow_redirects=False,
    )
    assert resp.status_code == 303 and "error=" not in resp.headers["location"]
    assert [m.to for m in capture.outbox] == ["jan@acme.cz"]
    history = await _latest_history(owner_engine, order_id)
    assert not _is_not_notified_marker(history.note)
    assert "notify_customer" not in await _status_audit(owner_engine, order_id)


async def test_unticked_checkbox_sends_no_customer_mail_and_records_it(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    [order_id] = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "owner@4mex.cz", "staffpass")
    capture = _capture(tenant_client)

    resp = await tenant_client.post(
        f"/app/orders/{order_id}/status",
        data={
            "to_status": "in_production",
            "note": "Material arrived",
            "notify_customer": NOTIFY_OFF,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303 and "error=" not in resp.headers["location"]
    assert capture.outbox == []

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        order = await session.get(Order, order_id)
    assert order.status == OrderStatus.IN_PRODUCTION

    history = await _latest_history(owner_engine, order_id)
    assert history.note.startswith("Material arrived; ")
    assert _is_not_notified_marker(history.note)
    after = await _status_audit(owner_engine, order_id)
    assert after["notify_customer"] is False
    assert _is_not_notified_marker(after["note"])


async def test_stepper_post_without_the_field_still_notifies(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """The one-click stepper buttons send no ``notify_customer``."""
    [order_id] = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "owner@4mex.cz", "staffpass")
    capture = _capture(tenant_client)

    resp = await tenant_client.post(
        f"/app/orders/{order_id}/transitions/in_production", follow_redirects=False
    )
    assert resp.status_code == 303
    assert [m.to for m in capture.outbox] == ["jan@acme.cz"]


async def test_opt_out_leaves_staff_notifications_alone(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """Staff submitting on the customer's behalf still mails the other
    staff (§19: STAFF_EVENTS); the opt-out is moot and not recorded."""
    [order_id] = await _seed(owner_engine, demo_tenant.id, status=OrderStatus.DRAFT)
    await _login(tenant_client, "owner@4mex.cz", "staffpass")
    capture = _capture(tenant_client)

    resp = await tenant_client.post(
        f"/app/orders/{order_id}/status",
        data={"to_status": "submitted", "notify_customer": NOTIFY_OFF},
        follow_redirects=False,
    )
    assert resp.status_code == 303 and "error=" not in resp.headers["location"]
    assert [m.to for m in capture.outbox] == ["operator@4mex.cz"]
    history = await _latest_history(owner_engine, order_id)
    assert not _is_not_notified_marker(history.note)
    assert "notify_customer" not in await _status_audit(owner_engine, order_id)


async def test_contact_action_ignores_the_field(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """A contact's own move mails the staff side as before, whatever is posted."""
    [order_id] = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "jan@acme.cz", "janpass")
    capture = _capture(tenant_client)

    resp = await tenant_client.post(
        f"/app/orders/{order_id}/transitions/cancelled",
        data={"notify_customer": NOTIFY_OFF},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert sorted(m.to for m in capture.outbox) == ["operator@4mex.cz", "owner@4mex.cz"]
    history = await _latest_history(owner_engine, order_id)
    assert not _is_not_notified_marker(history.note)
    assert "notify_customer" not in await _status_audit(owner_engine, order_id)


# -------------------------------------------------------------------- bulk


async def test_bulk_unticked_sends_no_customer_mail(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    order_ids = await _seed(owner_engine, demo_tenant.id, orders=2)
    await _login(tenant_client, "owner@4mex.cz", "staffpass")
    capture = _capture(tenant_client)

    resp = await tenant_client.post(
        "/app/orders/bulk/transition",
        data={
            "order_ids": [str(i) for i in order_ids],
            "to_status": "ready",
            "notify_customer": NOTIFY_OFF,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303 and "notice=" in resp.headers["location"]
    assert capture.outbox == []
    for order_id in order_ids:
        history = await _latest_history(owner_engine, order_id)
        assert history.to_status == OrderStatus.READY
        assert _is_not_notified_marker(history.note)
        assert (await _status_audit(owner_engine, order_id))["notify_customer"] is False


async def test_bulk_ticked_notifies_the_customer(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    order_ids = await _seed(owner_engine, demo_tenant.id, orders=2)
    await _login(tenant_client, "owner@4mex.cz", "staffpass")
    capture = _capture(tenant_client)

    resp = await tenant_client.post(
        "/app/orders/bulk/transition",
        data={
            "order_ids": [str(i) for i in order_ids],
            "to_status": "ready",
            "notify_customer": NOTIFY_ON,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # Two orders, one recipient -> one digest mail.
    assert [m.to for m in capture.outbox] == ["jan@acme.cz"]
    for order_id in order_ids:
        history = await _latest_history(owner_engine, order_id)
        assert not _is_not_notified_marker(history.note)


async def test_bulk_unticked_submit_still_mails_staff(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    order_ids = await _seed(owner_engine, demo_tenant.id, orders=2, status=OrderStatus.DRAFT)
    await _login(tenant_client, "owner@4mex.cz", "staffpass")
    capture = _capture(tenant_client)

    resp = await tenant_client.post(
        "/app/orders/bulk/transition",
        data={
            "order_ids": [str(i) for i in order_ids],
            "to_status": "submitted",
            "notify_customer": NOTIFY_OFF,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert [m.to for m in capture.outbox] == ["operator@4mex.cz"]
