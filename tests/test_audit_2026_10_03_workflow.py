"""Audit 2026-10-03 — deadlines, work queue, catalog, feed, materials,
and the "smart" order features.

T12      LOGIC-12 header edit, IDEA-4 promised date, overdue, BE-14 /
         LOGIC-19 / LOGIC-21 SLA semantics.
IDEA-1   "Needs action" queues; UX-05/07/08/12 list behaviour; BE-21 cap.
LOGIC-11 contact activity feed; UX-09 human labels.
LOGIC-14 customer price precedence; BE-15 catalog search.
LOGIC-22 notes on staff moves, no mail on corrections back to Draft.
LOGIC-24 customer material movements.
IDEA-2/3/5/6 reminders, order again, quote email, price memory.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from urllib.parse import unquote
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.email.sender import CaptureSender
from app.models.audit_event import AuditEvent
from app.models.enums import OrderStatus
from app.models.order import Order, OrderItem, OrderStatusHistory
from app.models.product import Product
from tests.test_orders_item_autosave import _login, _logout, _seed

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------- helpers


def _capture(client) -> CaptureSender:
    capture = CaptureSender()
    client._transport.app.state.email_sender = capture  # type: ignore[attr-defined]
    return capture


async def _order(owner_engine, order_id) -> Order:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        return (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()


async def _set(owner_engine, order_id, **values) -> None:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        order = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
        for key, value in values.items():
            setattr(order, key, value)


async def _new_order(client, customer_id, title: str = "Workflow") -> UUID:
    resp = await client.post(
        "/app/orders",
        data={"title": title, "customer_id": str(customer_id)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return UUID(resp.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0])


async def _add(client, order_id, *, price: str = "100", product_id: str = "", qty="1"):
    data = {"description": "Díl" if not product_id else "", "quantity": qty}
    if price:
        data["unit_price"] = price
    if product_id:
        data["product_id"] = product_id
    resp = await client.post(f"/app/orders/{order_id}/items", data=data, follow_redirects=False)
    assert resp.status_code == 303
    return resp


async def _move(client, order_id, status: str, **data):
    resp = await client.post(
        f"/app/orders/{order_id}/transitions/{status}", data=data or None, follow_redirects=False
    )
    assert resp.status_code == 303, resp.text
    assert "error=" not in resp.headers["location"], unquote(resp.headers["location"])
    return resp


async def _product(owner_engine, tenant_id, *, sku, name, price, customer_id=None, active=True):
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        product = Product(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=customer_id,
            sku=sku,
            name=name,
            unit="ks",
            default_price=Decimal(price) if price is not None else None,
            is_active=active,
        )
        session.add(product)
    return product


# ------------------------------------------------------- LOGIC-12 / IDEA-4


async def test_staff_can_edit_the_order_header_and_it_is_audited(
    tenant_client, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    order_id = await _new_order(tenant_client, seed["acme"].id)

    assert (await tenant_client.get(f"/app/orders/{order_id}/edit")).status_code == 200
    promised = (date.today() + timedelta(days=10)).isoformat()
    resp = await tenant_client.post(
        f"/app/orders/{order_id}/edit",
        data={
            "title": "Opravený název",
            "customer_id": str(seed["other"].id),
            "requested_delivery_at": "",
            "promised_delivery_at": promised,
            "notes": "Pozor na rozměry",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    order = await _order(owner_engine, order_id)
    assert order.title == "Opravený název"
    assert order.customer_id == seed["other"].id
    assert order.promised_delivery_at.isoformat() == promised

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.entity_id == order_id, AuditEvent.action == "order.updated"
                )
            )
        ).scalar_one()
    assert "promised_delivery_at" in event.diff["after"]


async def test_customer_cannot_be_changed_after_the_quote(
    tenant_client, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    order_id = await _new_order(tenant_client, seed["acme"].id)
    await _add(tenant_client, order_id)
    await _move(tenant_client, order_id, "confirmed")
    resp = await tenant_client.post(
        f"/app/orders/{order_id}/edit",
        data={"title": "X", "customer_id": str(seed["other"].id)},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert (await _order(owner_engine, order_id)).customer_id == seed["acme"].id


async def test_contact_cannot_edit_the_header(tenant_client, owner_engine, demo_tenant) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "jan@acme.cz", "contactpass")
    create = await tenant_client.post("/app/orders", data={"title": "Mine"}, follow_redirects=False)
    order_id = create.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0]
    resp = await tenant_client.post(
        f"/app/orders/{order_id}/edit",
        data={"title": "Hacked", "customer_id": str(seed["acme"].id)},
        follow_redirects=False,
    )
    assert resp.status_code in (401, 403)


async def test_promised_date_can_be_set_when_confirming_and_overdue_is_flagged(
    tenant_client, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    order_id = await _new_order(tenant_client, seed["acme"].id)
    await _add(tenant_client, order_id)
    await _move(tenant_client, order_id, "submitted")
    await _move(tenant_client, order_id, "quoted")
    page = await tenant_client.get(f"/app/orders/{order_id}")
    assert 'name="promised_delivery_at"' in page.text

    past = (date.today() - timedelta(days=2)).isoformat()
    await _move(
        tenant_client, order_id, "confirmed", promised_delivery_at=past, expected_total="100.00"
    )
    order = await _order(owner_engine, order_id)
    assert order.promised_delivery_at.isoformat() == past

    detail = await tenant_client.get(f"/app/orders/{order_id}")
    assert "data-overdue" in detail.text
    listing = await tenant_client.get("/app/orders")
    assert "data-overdue" in listing.text


async def test_contact_cannot_promise_a_date(tenant_client, owner_engine, demo_tenant) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    order_id = await _new_order(tenant_client, seed["acme"].id)
    await _add(tenant_client, order_id)
    await _move(tenant_client, order_id, "quoted")
    await _logout(tenant_client)
    await _login(tenant_client, "jan@acme.cz", "contactpass")
    await _move(
        tenant_client,
        order_id,
        "confirmed",
        expected_total="100.00",
        promised_delivery_at="2030-01-01",
    )
    assert (await _order(owner_engine, order_id)).promised_delivery_at is None


# ------------------------------------------------------------- SLA


async def _sla(demo_tenant):
    from app.services import sla_service
    from tests.test_sla_service import _app_session

    engine, sm = await _app_session(demo_tenant.id)
    try:
        async with sm() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(demo_tenant.id)},
            )
            return await sla_service.on_time_rate(
                session,
                date_from=date.today() - timedelta(days=30),
                date_to=date.today() + timedelta(days=30),
            )
    finally:
        await engine.dispose()


async def test_sla_excludes_cancelled_and_draft_to_closed_jumps(
    tenant_client, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    promised = date.today() + timedelta(days=5)

    delivered = await _new_order(tenant_client, seed["acme"].id, "Delivered")
    await _add(tenant_client, delivered)
    await _set(owner_engine, delivered, promised_delivery_at=promised)
    await _move(tenant_client, delivered, "delivered")

    bookkeeping = await _new_order(tenant_client, seed["acme"].id, "Closed by phone")
    await _set(owner_engine, bookkeeping, promised_delivery_at=promised)
    await _move(tenant_client, bookkeeping, "closed")
    # §18: the backfill still stamps delivered_at …
    assert (await _order(owner_engine, bookkeeping)).delivered_at is not None

    cancelled = await _new_order(tenant_client, seed["acme"].id, "Cancelled")
    await _set(
        owner_engine,
        cancelled,
        promised_delivery_at=date.today() - timedelta(days=3),
    )
    await _move(tenant_client, cancelled, "cancelled")

    result = await _sla(demo_tenant)
    # … but only the genuinely delivered order counts; the cancelled
    # overdue one is not "pending" either.
    assert result["on_time"] == 1
    assert result["total"] == 1
    assert result["pending"] == 0


# ------------------------------------------------------- IDEA-1 / UX-12


async def test_dashboard_needs_action_queues_link_to_filtered_lists(
    tenant_client, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")

    submitted = await _new_order(tenant_client, seed["acme"].id, "Needs a quote")
    await _add(tenant_client, submitted)
    await _move(tenant_client, submitted, "submitted")

    overdue = await _new_order(tenant_client, seed["acme"].id, "Late one")
    await _add(tenant_client, overdue)
    await _move(tenant_client, overdue, "in_production")
    await _set(owner_engine, overdue, promised_delivery_at=date.today() - timedelta(days=1))

    no_promise = await _new_order(tenant_client, seed["acme"].id, "No date")
    await _add(tenant_client, no_promise)
    await _move(tenant_client, no_promise, "confirmed")

    stale = await _new_order(tenant_client, seed["acme"].id, "Old quote")
    await _add(tenant_client, stale)
    await _move(tenant_client, stale, "quoted")
    await _set(owner_engine, stale, quoted_at=datetime.now(UTC) - timedelta(days=10))

    body = (await tenant_client.get("/app")).text
    for key in ("awaiting_quote", "no_promise", "overdue", "stale_quotes"):
        assert f'data-work-queue="{key}"' in body
        assert f"/app/orders?queue={key}" in body

    expectations = {
        "awaiting_quote": "Needs a quote",
        "overdue": "Late one",
        "no_promise": "No date",
        "stale_quotes": "Old quote",
    }
    for key, title in expectations.items():
        listing = (await tenant_client.get(f"/app/orders?queue={key}")).text
        assert title in listing, key
        others = [t for k, t in expectations.items() if k != key and t != title]
        assert not any(o in listing for o in others), (key, others)


async def test_contact_dashboard_shows_quotes_awaiting_confirmation(
    tenant_client, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    order_id = await _new_order(tenant_client, seed["acme"].id)
    await _add(tenant_client, order_id)
    await _move(tenant_client, order_id, "quoted")
    await _logout(tenant_client)
    await _login(tenant_client, "jan@acme.cz", "contactpass")
    body = (await tenant_client.get("/app")).text
    assert 'data-work-queue="awaiting_confirmation"' in body
    # Contacts never get the supplier's queues.
    assert "queue=overdue" not in body


async def test_list_sorts_by_due_date_and_links_rows(
    tenant_client, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    later = await _new_order(tenant_client, seed["acme"].id, "Due later")
    sooner = await _new_order(tenant_client, seed["acme"].id, "Due sooner")
    undated = await _new_order(tenant_client, seed["acme"].id, "Undated")
    await _set(owner_engine, later, promised_delivery_at=date.today() + timedelta(days=20))
    await _set(owner_engine, sooner, promised_delivery_at=date.today() + timedelta(days=2))

    body = (await tenant_client.get("/app/orders?sort=due")).text
    assert body.index("Due sooner") < body.index("Due later") < body.index("Undated")
    # UX-05: the number is a real link; UX-07: the bulk form confirms.
    assert f'<a href="/app/orders/{sooner}"' in body
    assert "data-bulk-form" in body and "{count}" in body
    assert undated  # created, just undated


async def test_pagination_keeps_the_assigned_filter(
    tenant_client, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    for i in range(21):
        order_id = await _new_order(tenant_client, seed["acme"].id, f"Mine {i}")
        await tenant_client.post(
            f"/app/orders/{order_id}/assign",
            data={"assigned_to": str(seed["staff"].id)},
            follow_redirects=False,
        )
    body = (await tenant_client.get("/app/orders?assigned=me")).text
    assert "page=2&amp;assigned=me" in body or "page=2&assigned=me" in body


async def test_bulk_transition_is_capped(tenant_client, owner_engine, demo_tenant) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    resp = await tenant_client.post(
        "/app/orders/bulk/transition",
        data={"order_ids": [str(uuid4()) for _ in range(201)], "to_status": "submitted"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]


# ------------------------------------------------------- LOGIC-11 / UX-09


async def test_contact_feed_hides_internal_comments_and_assignment(
    tenant_client, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    order_id = await _new_order(tenant_client, seed["acme"].id)
    await tenant_client.post(
        f"/app/orders/{order_id}/comments",
        data={"body": "Interní poznámka", "is_internal": "1"},
        follow_redirects=False,
    )
    await tenant_client.post(
        f"/app/orders/{order_id}/assign",
        data={"assigned_to": str(seed["staff"].id)},
        follow_redirects=False,
    )
    await tenant_client.post(
        f"/app/orders/{order_id}/comments",
        data={"body": "Veřejná poznámka"},
        follow_redirects=False,
    )
    staff_body = (await tenant_client.get("/app")).text
    assert "order.assigned" not in staff_body, "UX-09: no raw action codes"
    await _logout(tenant_client)

    from app.services import audit_service

    class _Contact:
        is_staff = False
        customer_id = seed["acme"].id

    from tests.test_sla_service import _app_session

    engine, sm = await _app_session(demo_tenant.id)
    try:
        async with sm() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(demo_tenant.id)},
            )
            events = await audit_service.list_recent(session, principal=_Contact(), limit=50)
    finally:
        await engine.dispose()
    actions = [(e.action, (e.diff or {}).get("after", {}).get("is_internal")) for e in events]
    assert ("order.assigned", None) not in actions
    assert ("order.comment_added", True) not in actions
    assert ("order.comment_added", False) in actions


# ------------------------------------------------------ LOGIC-14 / BE-15


async def test_customer_price_wins_over_the_shared_sku(
    tenant_client, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)
    shared = await _product(owner_engine, demo_tenant.id, sku="BOLT", name="Šroub", price="10")
    special = await _product(
        owner_engine,
        demo_tenant.id,
        sku="BOLT",
        name="Šroub (ACME)",
        price="8",
        customer_id=seed["acme"].id,
    )
    foreign = await _product(
        owner_engine,
        demo_tenant.id,
        sku="NUT",
        name="Matice (Other)",
        price="1",
        customer_id=seed["other"].id,
    )
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    order_id = await _new_order(tenant_client, seed["acme"].id)

    page = (await tenant_client.get(f"/app/orders/{order_id}")).text
    assert str(special.id) in page
    assert str(shared.id) not in page, "the shared row is hidden for ACME"
    assert str(foreign.id) not in page

    # Posting the shared id still lands the customer's price.
    await _add(tenant_client, order_id, price="", product_id=str(shared.id))
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        item = (
            await session.execute(select(OrderItem).where(OrderItem.order_id == order_id))
        ).scalar_one()
    assert item.unit_price == Decimal("8.00")
    assert item.product_id == special.id

    # Another customer's product is refused for staff too.
    resp = await _add(tenant_client, order_id, price="", product_id=str(foreign.id))
    assert "error=" in resp.headers["location"]


async def test_catalog_picker_searches_beyond_the_first_page(
    tenant_client, owner_engine, demo_tenant, monkeypatch
) -> None:
    from app.routers import orders as orders_router

    monkeypatch.setattr(orders_router, "PRODUCT_PICKER_LIMIT", 3)
    seed = await _seed(owner_engine, demo_tenant.id)
    for i in range(5):
        await _product(owner_engine, demo_tenant.id, sku=f"A{i}", name=f"Alpha {i}", price="1")
    late = await _product(owner_engine, demo_tenant.id, sku="Z9", name="Zeta", price="1")

    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    order_id = await _new_order(tenant_client, seed["acme"].id)
    page = (await tenant_client.get(f"/app/orders/{order_id}")).text
    assert 'name="product_q"' in page and "data-product-count" in page
    assert str(late.id) not in page

    found = (await tenant_client.get(f"/app/orders/{order_id}?product_q=zeta")).text
    assert str(late.id) in found


# -------------------------------------------------------------- IDEA-6


async def test_picker_prefills_the_last_price_for_this_customer(
    tenant_client, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)
    product = await _product(owner_engine, demo_tenant.id, sku="P1", name="Pouzdro", price="50")
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    first = await _new_order(tenant_client, seed["acme"].id)
    await _add(tenant_client, first, price="42.50", product_id=str(product.id))

    second = await _new_order(tenant_client, seed["acme"].id)
    page = (await tenant_client.get(f"/app/orders/{second}")).text
    assert 'data-price="42.50"' in page

    other = await _new_order(tenant_client, seed["other"].id)
    page = (await tenant_client.get(f"/app/orders/{other}")).text
    assert 'data-price="50.00"' in page, "another customer's price must not leak"


# ------------------------------------------------------------ LOGIC-22


async def test_status_note_reaches_history_and_mail_and_draft_is_silent(
    tenant_client, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    order_id = await _new_order(tenant_client, seed["acme"].id)
    await _add(tenant_client, order_id)
    await _move(tenant_client, order_id, "in_production")

    capture = _capture(tenant_client)
    resp = await tenant_client.post(
        f"/app/orders/{order_id}/status",
        data={"to_status": "ready", "note": "Hotovo dřív"},
        follow_redirects=False,
    )
    assert "error=" not in resp.headers["location"]
    assert capture.outbox and "Hotovo dřív" in capture.outbox[0].text

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        note = (
            await session.execute(
                select(OrderStatusHistory.note).where(
                    OrderStatusHistory.order_id == order_id,
                    OrderStatusHistory.to_status == OrderStatus.READY,
                )
            )
        ).scalar_one()
    assert note == "Hotovo dřív"

    capture.outbox.clear()
    await _move(tenant_client, order_id, "draft")
    assert capture.outbox == [], "a correction back to Draft must not email the customer"


# --------------------------------------------------------------- IDEA-5


async def test_quote_email_carries_total_and_confirm_link(
    tenant_client, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    order_id = await _new_order(tenant_client, seed["acme"].id)
    await _add(tenant_client, order_id, price="1250")
    capture = _capture(tenant_client)
    await _move(tenant_client, order_id, "quoted")
    mail = next(m for m in capture.outbox if m.to == "jan@acme.cz")
    assert "1 250" in mail.text
    assert f"/app/orders/{order_id}#order-status" in mail.text


# --------------------------------------------------------------- IDEA-3


async def test_order_again_copies_lines_with_current_prices(
    tenant_client, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)
    product = await _product(owner_engine, demo_tenant.id, sku="K1", name="Konzola", price="30")
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    source = await _new_order(tenant_client, seed["acme"].id, "Original")
    await _add(tenant_client, source, price="25", product_id=str(product.id), qty="4")
    await _add(tenant_client, source, price="999")  # free-text, re-quote
    await _move(tenant_client, source, "delivered")
    await _logout(tenant_client)

    await _login(tenant_client, "jan@acme.cz", "contactpass")
    resp = await tenant_client.post(f"/app/orders/{source}/duplicate", follow_redirects=False)
    assert resp.status_code == 303
    new_id = UUID(resp.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0])
    new = await _order(owner_engine, new_id)
    assert new.status == OrderStatus.DRAFT
    assert new.customer_id == seed["acme"].id
    assert new.created_by_contact_id == seed["jan"].id

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        items = (
            (
                await session.execute(
                    select(OrderItem)
                    .where(OrderItem.order_id == new_id)
                    .order_by(OrderItem.position)
                )
            )
            .scalars()
            .all()
        )
    assert [i.quantity for i in items] == [Decimal("4.000"), Decimal("1.000")]
    assert items[0].unit_price == Decimal("30.00"), "catalog lines take today's list price"
    assert items[1].unit_price is None, "free-text lines are re-quoted"
    await _logout(tenant_client)

    # Another customer's contact cannot reorder it.
    await _login(tenant_client, "eva@other.cz", "evapass")
    resp = await tenant_client.post(f"/app/orders/{source}/duplicate", follow_redirects=False)
    assert resp.status_code == 404


# --------------------------------------------------------------- IDEA-2


async def test_quote_reminder_is_sent_once_and_respects_consent(
    tenant_client, owner_engine, demo_tenant, monkeypatch
) -> None:
    from app.config import get_settings
    from app.tasks.quote_reminders import send_quote_reminders

    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    order_id = await _new_order(tenant_client, seed["acme"].id)
    await _add(tenant_client, order_id, price="10")
    await _move(tenant_client, order_id, "quoted")

    fresh = await _new_order(tenant_client, seed["acme"].id, "Fresh quote")
    await _add(tenant_client, fresh, price="10")
    await _move(tenant_client, fresh, "quoted")

    await _set(owner_engine, order_id, quoted_at=datetime.now(UTC) - timedelta(days=4))
    monkeypatch.setenv("QUOTE_REMINDER_DAYS", "3")
    get_settings.cache_clear()

    outbox = CaptureSender()
    assert await send_quote_reminders(sender=outbox) == 1
    assert [m.to for m in outbox.outbox] == ["jan@acme.cz"]
    assert f"/app/orders/{order_id}#order-status" in outbox.outbox[0].text

    # Second run: nothing new.
    assert await send_quote_reminders(sender=CaptureSender()) == 0

    # A re-quote re-arms it — but a contact who opted out hears nothing.
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        from app.models.customer import CustomerContact

        jan = (
            await session.execute(
                select(CustomerContact).where(CustomerContact.id == seed["jan"].id)
            )
        ).scalar_one()
        jan.notification_prefs = {"events": {"quote_reminder": False}}
    await _set(
        owner_engine,
        order_id,
        quoted_at=datetime.now(UTC) - timedelta(days=5),
        quote_reminder_sent_at=datetime.now(UTC) - timedelta(days=6),
    )
    silent = CaptureSender()
    assert await send_quote_reminders(sender=silent) == 1
    assert silent.outbox == []


async def test_quote_reminder_disabled_with_zero_days(monkeypatch) -> None:
    from app.config import get_settings
    from app.tasks.quote_reminders import send_quote_reminders

    monkeypatch.setenv("QUOTE_REMINDER_DAYS", "0")
    get_settings.cache_clear()
    assert await send_quote_reminders(sender=CaptureSender()) == 0


# ------------------------------------------------------------ LOGIC-24


async def test_material_movements_are_validated_and_audited(
    tenant_client, owner_engine, demo_tenant
) -> None:
    from app.models.asset import Asset

    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    other_order = await _new_order(tenant_client, seed["other"].id)
    own_order = await _new_order(tenant_client, seed["acme"].id)

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        asset = Asset(
            id=uuid4(),
            tenant_id=demo_tenant.id,
            customer_id=seed["acme"].id,
            code="M-1",
            name="Ocelová tyč",
            unit="m",
        )
        session.add(asset)

    async def _post(**data):
        return await tenant_client.post(
            f"/app/assets/{asset.id}/movements", data=data, follow_redirects=False
        )

    ok = await _post(type="receive", quantity="10", reference_order_id=str(own_order))
    assert "error=" not in ok.headers["location"]
    foreign = await _post(type="consume", quantity="1", reference_order_id=str(other_order))
    assert "error=" in foreign.headers["location"]
    made_up = await _post(type="consume", quantity="1", reference_order_id=str(uuid4()))
    assert made_up.status_code == 303 and "error=" in made_up.headers["location"]
    negative = await _post(type="adjust", quantity="-11")
    assert "error=" in negative.headers["location"]
    nan = await _post(type="receive", quantity="NaN")
    assert "error=" in nan.headers["location"]

    async with sm() as session:
        stock = (
            await session.execute(select(Asset.current_quantity).where(Asset.id == asset.id))
        ).scalar_one()
        audited = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.action == "asset.movement_added")
                )
            )
            .scalars()
            .all()
        )
    assert stock == Decimal("10.000")
    assert len(audited) == 1
