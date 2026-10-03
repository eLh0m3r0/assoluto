"""Audit 2026-10-03 T7 — quote integrity.

LOGIC-2  confirmation is bound to the amount the customer saw, and the
         agreed amount is snapshotted.
LOGIC-7  no quote / confirmation of unpriced or empty orders; no empty
         submissions.
LOGIC-13 attachment deletion is restricted and audited.
LOGIC-17 half-up rounding on the PDF.
UX-06    destructive item / drawing deletes ask for confirmation.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.audit_event import AuditEvent
from app.models.enums import OrderStatus
from app.models.order import Order, OrderItem, OrderStatusHistory
from app.services.pdf_service import format_money
from tests.test_orders_item_autosave import _login, _logout, _seed

pytestmark = pytest.mark.postgres


@pytest.fixture
def s3_mock(monkeypatch):  # type: ignore[misc]
    """In-process moto S3 (same shape as test_notifications_flow.mock_s3)."""
    import boto3
    from moto import mock_aws

    from app.config import get_settings
    from app.storage import s3 as s3_mod

    monkeypatch.setenv("S3_ENDPOINT_URL", "")
    monkeypatch.setenv("S3_ACCESS_KEY", "test")
    monkeypatch.setenv("S3_SECRET_KEY", "test")
    monkeypatch.setenv("S3_BUCKET", "portal-quote-test")
    get_settings.cache_clear()
    bucket = get_settings().s3_bucket

    with mock_aws():
        s3_mod.get_s3_client.cache_clear()
        boto3.client("s3", region_name="eu-central-1").create_bucket(
            Bucket=bucket,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )
        yield
        s3_mod.get_s3_client.cache_clear()


async def _order(owner_engine, order_id) -> Order:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        return (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()


async def _new_order(client, customer_id) -> UUID:
    resp = await client.post(
        "/app/orders",
        data={"title": "Quote integrity", "customer_id": str(customer_id)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return UUID(resp.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0])


async def _add(client, order_id, *, price: str = "", qty: str = "2") -> None:
    data = {"description": "Díl", "quantity": qty}
    if price:
        data["unit_price"] = price
    resp = await client.post(f"/app/orders/{order_id}/items", data=data, follow_redirects=False)
    assert resp.status_code == 303 and "error=" not in resp.headers["location"], resp.headers


async def _quoted_order(client, owner_engine, seed, *, price: str = "100") -> UUID:
    """Staff creates an order for ACME with one priced line and quotes it."""
    await _login(client, "staff@4mex.cz", "staffpass")
    order_id = await _new_order(client, seed["acme"].id)
    await _add(client, order_id, price=price)
    resp = await client.post(f"/app/orders/{order_id}/transitions/quoted", follow_redirects=False)
    assert "error=" not in resp.headers["location"]
    await _logout(client)
    return order_id


# ------------------------------------------------------------------ LOGIC-2


async def test_contact_confirmation_snapshots_the_agreed_amount(
    tenant_client, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)
    order_id = await _quoted_order(tenant_client, owner_engine, seed)

    await _login(tenant_client, "jan@acme.cz", "contactpass")
    page = await tenant_client.get(f"/app/orders/{order_id}")
    assert 'name="expected_total" value="200.00"' in page.text

    resp = await tenant_client.post(
        f"/app/orders/{order_id}/transitions/confirmed",
        data={"expected_total": "200.00"},
        follow_redirects=False,
    )
    assert "error=" not in resp.headers["location"]

    order = await _order(owner_engine, order_id)
    assert order.status == OrderStatus.CONFIRMED
    assert order.confirmed_total == Decimal("200.00")
    assert order.confirmed_at is not None
    assert order.confirmed_by_contact_id == seed["jan"].id
    assert order.confirmed_by_user_id is None

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
    confirm_events = [
        e for e in events if (e.diff or {}).get("after", {}).get("status") == "confirmed"
    ]
    assert confirm_events and confirm_events[0].diff["after"]["confirmed_total"] == "200.00"

    detail = await tenant_client.get(f"/app/orders/{order_id}")
    assert "data-confirmed-total" in detail.text


async def test_confirming_a_stale_quote_is_refused(
    tenant_client, owner_engine, demo_tenant
) -> None:
    """Customer opens the quote at 200, staff re-prices to 300, the
    customer clicks Confirm on the stale page: nothing must be agreed."""
    seed = await _seed(owner_engine, demo_tenant.id)
    order_id = await _quoted_order(tenant_client, owner_engine, seed)

    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        item_id = (
            await session.execute(select(OrderItem.id).where(OrderItem.order_id == order_id))
        ).scalar_one()
    patch = await tenant_client.post(
        f"/app/orders/{order_id}/items/{item_id}/patch", data={"unit_price": "150"}
    )
    assert patch.status_code == 200
    await _logout(tenant_client)

    await _login(tenant_client, "jan@acme.cz", "contactpass")
    resp = await tenant_client.post(
        f"/app/orders/{order_id}/transitions/confirmed",
        data={"expected_total": "200.00"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    order = await _order(owner_engine, order_id)
    assert order.status == OrderStatus.QUOTED
    assert order.confirmed_at is None


async def test_contact_confirm_without_a_displayed_total_is_refused(
    tenant_client, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)
    order_id = await _quoted_order(tenant_client, owner_engine, seed)
    await _login(tenant_client, "jan@acme.cz", "contactpass")
    resp = await tenant_client.post(
        f"/app/orders/{order_id}/transitions/confirmed", follow_redirects=False
    )
    assert "error=" in resp.headers["location"]
    assert (await _order(owner_engine, order_id)).status == OrderStatus.QUOTED


async def test_staff_jump_past_confirmed_backfills_the_snapshot(
    tenant_client, owner_engine, demo_tenant
) -> None:
    """A phone-agreed DRAFT -> IN_PRODUCTION jump is still an agreement
    (CLAUDE.md §18: side effects of skipped steps are backfilled)."""
    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    order_id = await _new_order(tenant_client, seed["acme"].id)
    await _add(tenant_client, order_id, price="50", qty="3")
    resp = await tenant_client.post(
        f"/app/orders/{order_id}/transitions/in_production", follow_redirects=False
    )
    assert "error=" not in resp.headers["location"]
    order = await _order(owner_engine, order_id)
    assert order.confirmed_total == Decimal("150.00")
    assert order.confirmed_by_user_id == seed["staff"].id
    assert order.quoted_at is not None


# ------------------------------------------------------------------ LOGIC-7


async def test_contact_cannot_submit_an_empty_order(
    tenant_client, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "jan@acme.cz", "contactpass")
    create = await tenant_client.post(
        "/app/orders", data={"title": "Empty"}, follow_redirects=False
    )
    order_id = UUID(create.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0])
    resp = await tenant_client.post(
        f"/app/orders/{order_id}/transitions/submitted", follow_redirects=False
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    assert (await _order(owner_engine, order_id)).status == OrderStatus.DRAFT


async def test_unpriced_quote_needs_an_explicit_staff_override(
    tenant_client, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    order_id = await _new_order(tenant_client, seed["acme"].id)
    await _add(tenant_client, order_id, price="10")
    await _add(tenant_client, order_id)  # unpriced

    refused = await tenant_client.post(
        f"/app/orders/{order_id}/transitions/quoted", follow_redirects=False
    )
    assert "error=" in refused.headers["location"]
    assert (await _order(owner_engine, order_id)).status == OrderStatus.DRAFT

    # The stepper offers the override behind a "continue anyway?" confirm.
    page = await tenant_client.get(f"/app/orders/{order_id}")
    assert 'name="allow_incomplete" value="1"' in page.text

    forced = await tenant_client.post(
        f"/app/orders/{order_id}/transitions/quoted",
        data={"allow_incomplete": "1"},
        follow_redirects=False,
    )
    assert "error=" not in forced.headers["location"]
    assert (await _order(owner_engine, order_id)).status == OrderStatus.QUOTED

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        notes = (
            (
                await session.execute(
                    select(OrderStatusHistory.note).where(
                        OrderStatusHistory.order_id == order_id,
                        OrderStatusHistory.to_status == OrderStatus.QUOTED,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert notes and notes[0], "the override must be recorded in the history"

    # The customer can never confirm it, override or not.
    await _logout(tenant_client)
    await _login(tenant_client, "jan@acme.cz", "contactpass")
    page = await tenant_client.get(f"/app/orders/{order_id}")
    assert "/transitions/confirmed" not in page.text
    resp = await tenant_client.post(
        f"/app/orders/{order_id}/transitions/confirmed",
        data={"expected_total": "20.00", "allow_incomplete": "1"},
        follow_redirects=False,
    )
    assert "error=" in resp.headers["location"]
    assert (await _order(owner_engine, order_id)).status == OrderStatus.QUOTED


# ----------------------------------------------------------- LOGIC-13 / UX-06


async def test_contact_cannot_delete_a_supplier_drawing_and_deletes_are_audited(
    tenant_client, owner_engine, demo_tenant, s3_mock
) -> None:
    from app.models.attachment import OrderAttachment
    from tests.test_attachments_flow import _png_bytes

    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    order_id = await _new_order(tenant_client, seed["acme"].id)
    upload = await tenant_client.post(
        f"/app/orders/{order_id}/attachments",
        files={"file": ("vykres.png", _png_bytes(), "image/png")},
        follow_redirects=False,
    )
    assert upload.status_code == 303
    page = await tenant_client.get(f"/app/orders/{order_id}")
    assert "/attachments/" in page.text and "data-confirm" in page.text
    await _logout(tenant_client)

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        att = (
            await session.execute(
                select(OrderAttachment).where(OrderAttachment.order_id == order_id)
            )
        ).scalar_one()

    await _login(tenant_client, "jan@acme.cz", "contactpass")
    resp = await tenant_client.post(f"/app/attachments/{att.id}/delete", follow_redirects=False)
    assert resp.status_code == 303 and "error=" in resp.headers["location"]
    await _logout(tenant_client)

    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    resp = await tenant_client.post(f"/app/attachments/{att.id}/delete", follow_redirects=False)
    assert resp.status_code == 303 and "error=" not in resp.headers["location"]

    async with sm() as session:
        gone = (
            await session.execute(select(OrderAttachment).where(OrderAttachment.id == att.id))
        ).scalar_one_or_none()
        audit = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.action == "attachment.deleted")
                )
            )
            .scalars()
            .all()
        )
    assert gone is None
    assert len(audit) == 1
    assert audit[0].diff["before"]["filename"] == "vykres.png"
    assert audit[0].entity_id == order_id


async def test_item_delete_asks_for_confirmation(tenant_client, owner_engine, demo_tenant) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    order_id = await _new_order(tenant_client, seed["acme"].id)
    await _add(tenant_client, order_id, price="10")
    body = (await tenant_client.get(f"/app/orders/{order_id}")).text
    marker = body.index("/delete")
    form = body[body.rindex("<form", 0, marker) : body.index(">", marker)]
    assert "data-confirm" in form


# ------------------------------------------------------------------ LOGIC-17


def test_pdf_money_rounds_half_up() -> None:
    assert format_money(Decimal("0.125")) == "0.13"
    assert format_money(Decimal("NaN")) == ""
