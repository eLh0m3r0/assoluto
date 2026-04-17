"""Full end-to-end test walking the 26-step acceptance scenario from the plan.

This test chains every feature together so regressions in the big
picture get caught in a single place:
    create_tenant → login → customer → contact invite → accept →
    product catalog → draft order → add items → attach file →
    submit → staff quote → contact confirm → staff transitions →
    assets + movements → contact sees only its own data →
    cross-tenant isolation → oversize upload rejection →
    auto-close after 14 days

Notifications are captured so we can assert the inbox along the way.
"""

from __future__ import annotations

import io
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from freezegun import freeze_time
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.email.sender import CaptureSender
from app.models.asset import Asset, AssetMovement
from app.models.customer import Customer
from app.models.enums import OrderStatus
from app.models.order import Order
from app.models.product import Product
from app.models.tenant import Tenant
from app.models.user import User
from app.security.passwords import hash_password

pytestmark = pytest.mark.postgres


@pytest.fixture
def mock_s3(settings):  # type: ignore[misc]
    """Start moto mock S3 and pre-create the bucket."""
    import boto3
    from moto import mock_aws

    from app.storage import s3 as s3_mod

    with mock_aws():
        s3_mod.get_s3_client.cache_clear()
        s3_mod.get_public_s3_client.cache_clear()
        client = boto3.client("s3", region_name="eu-central-1")
        client.create_bucket(
            Bucket=settings.s3_bucket,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )
        yield
        s3_mod.get_s3_client.cache_clear()
        s3_mod.get_public_s3_client.cache_clear()


@pytest.fixture(autouse=True)
def _e2e_s3_env(monkeypatch):  # type: ignore[misc]
    monkeypatch.setenv("S3_ENDPOINT_URL", "")
    monkeypatch.setenv("S3_PUBLIC_ENDPOINT_URL", "")
    monkeypatch.setenv("S3_ACCESS_KEY", "test")
    monkeypatch.setenv("S3_SECRET_KEY", "test")
    monkeypatch.setenv("S3_BUCKET", "portal-e2e")
    monkeypatch.setenv("S3_REGION", "eu-central-1")
    yield


def _png(color=(120, 40, 220)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (60, 60), color=color).save(buf, format="PNG")
    return buf.getvalue()


async def _login(client: AsyncClient, email: str, password: str) -> None:
    resp = await client.post(
        "/auth/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"{email} login failed: {resp.text}"


async def _logout(client: AsyncClient) -> None:
    await client.post("/auth/logout", follow_redirects=False)
    client.cookies.clear()


async def test_plan_e2e_happy_path(
    tenant_client: AsyncClient,
    owner_engine,
    demo_tenant,
    mock_s3,
) -> None:
    """Walks steps 1-17 plus 24-25 of the plan's acceptance scenario."""
    capture = CaptureSender()
    tenant_client._transport.app.state.email_sender = capture  # type: ignore[attr-defined]

    # -------- Step 1: bootstrap tenant owner (direct seed for speed) --------
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        session.add(
            User(
                id=uuid4(),
                tenant_id=demo_tenant.id,
                email="owner@4mex.cz",
                full_name="4MEX Owner",
                role=__import__("app.models.enums", fromlist=["UserRole"]).UserRole.TENANT_ADMIN,
                password_hash=hash_password("demo1234"),
            )
        )

    # -------- Steps 2-3: owner logs in, invites Výroba staff --------------
    await _login(tenant_client, "owner@4mex.cz", "demo1234")

    assert (await tenant_client.get("/app")).status_code == 200

    invite_staff = await tenant_client.post(
        "/app/admin/users/invite",
        data={
            "email": "vyroba@4mex.cz",
            "full_name": "Výroba",
            "role": "tenant_staff",
        },
        follow_redirects=False,
    )
    assert invite_staff.status_code == 303

    assert any(m.to == "vyroba@4mex.cz" for m in capture.outbox)

    # -------- Step 4: create customer ACME ---------------------------------
    acme_resp = await tenant_client.post(
        "/app/customers",
        data={
            "name": "ACME s.r.o.",
            "ico": "12345678",
            "dic": "CZ12345678",
            "notes": "",
        },
        follow_redirects=False,
    )
    assert acme_resp.status_code == 303
    acme_id = UUID(acme_resp.headers["location"].rsplit("/", 1)[-1])

    # -------- Step 5: invite customer contact jan@acme.cz ------------------
    before_invites = len(capture.outbox)
    invite_contact = await tenant_client.post(
        f"/app/customers/{acme_id}/contacts",
        data={"email": "jan@acme.cz", "full_name": "Jan Novák"},
        follow_redirects=False,
    )
    assert invite_contact.status_code == 303
    invite_emails = [m for m in capture.outbox[before_invites:] if m.to == "jan@acme.cz"]
    assert len(invite_emails) == 1
    token_match = re.search(r"/invite/accept\?token=([\w\-.]+)", invite_emails[0].html)
    assert token_match
    invite_token = token_match.group(1)

    # -------- Step 6: Jan accepts the invite & auto-logs-in ----------------
    tenant_client.cookies.clear()

    accept_get = await tenant_client.get(f"/invite/accept?token={invite_token}")
    assert accept_get.status_code == 200
    assert "Jan Novák" in accept_get.text

    accept_post = await tenant_client.post(
        "/invite/accept",
        data={
            "token": invite_token,
            "password": "janpass99",
            "password_confirm": "janpass99",
        },
        follow_redirects=False,
    )
    assert accept_post.status_code == 303

    # -------- Step 7: contact dashboard is scoped (no admin link) ----------
    dashboard = await tenant_client.get("/app")
    assert dashboard.status_code == 200
    assert "Tým" not in dashboard.text  # no admin nav
    assert "/app/customers" not in dashboard.text  # customers is staff-only

    # -------- Step 8 (partial): owner creates 3 shared + 1 private product -
    await _logout(tenant_client)
    await _login(tenant_client, "owner@4mex.cz", "demo1234")
    for sku, name in [("SKU-100", "Plech 2mm"), ("SKU-101", "TIG svar"), ("SKU-102", "Lakování")]:
        resp = await tenant_client.post(
            "/app/products",
            data={"sku": sku, "name": name, "unit": "ks"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
    await tenant_client.post(
        "/app/products",
        data={
            "sku": "ACME-001",
            "name": "Custom výkres",
            "unit": "ks",
            "default_price": "1200.00",
            "customer_id": str(acme_id),
        },
        follow_redirects=False,
    )

    # -------- Step 9-10: Jan creates order with a product + free-text -----
    await _logout(tenant_client)
    await _login(tenant_client, "jan@acme.cz", "janpass99")

    create_order = await tenant_client.post(
        "/app/orders",
        data={"title": "Q1 zakázka", "notes": ""},
        follow_redirects=False,
    )
    assert create_order.status_code == 303
    order_id = UUID(create_order.headers["location"].rsplit("/", 1)[-1])

    # Pull the ACME product UUID out via owner engine.
    async with sm() as session:
        acme_product = (
            await session.execute(select(Product).where(Product.sku == "ACME-001"))
        ).scalar_one()

    add_item_a = await tenant_client.post(
        f"/app/orders/{order_id}/items",
        data={
            "product_id": str(acme_product.id),
            "quantity": "10",
        },
        follow_redirects=False,
    )
    assert add_item_a.status_code == 303

    add_item_b = await tenant_client.post(
        f"/app/orders/{order_id}/items",
        data={
            "description": "Řezání plechu dle výkresu",
            "quantity": "5",
            "unit": "ks",
        },
        follow_redirects=False,
    )
    assert add_item_b.status_code == 303

    # Upload a drawing attachment.
    upload = await tenant_client.post(
        f"/app/orders/{order_id}/attachments",
        files={"file": ("vykres.png", _png(), "image/png")},
        follow_redirects=False,
    )
    assert upload.status_code == 303

    # -------- Step 12: Jan submits the order -------------------------------
    submit_inbox_before = len(capture.outbox)
    submit = await tenant_client.post(
        f"/app/orders/{order_id}/transitions/submitted",
        follow_redirects=False,
    )
    assert submit.status_code == 303

    # -------- Step 13: owner gets "Nová objednávka" email ------------------
    submit_mails = [
        m for m in capture.outbox[submit_inbox_before:] if "Nová objednávka" in m.subject
    ]
    assert len(submit_mails) >= 1
    assert any(m.to == "owner@4mex.cz" for m in submit_mails)

    async with sm() as session:
        o = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
    assert o.status == OrderStatus.SUBMITTED
    assert o.submitted_at is not None

    # -------- Step 14: owner adds price + quotes the order -----------------
    await _logout(tenant_client)
    await _login(tenant_client, "owner@4mex.cz", "demo1234")

    # Staff patches the free-text line by adding a brand new priced line.
    await tenant_client.post(
        f"/app/orders/{order_id}/items",
        data={
            "description": "Doprava",
            "quantity": "1",
            "unit": "ks",
            "unit_price": "850",
        },
        follow_redirects=False,
    )
    before_quote = len(capture.outbox)
    quote = await tenant_client.post(
        f"/app/orders/{order_id}/transitions/quoted",
        follow_redirects=False,
    )
    assert quote.status_code == 303

    # Customer contact gets a status-change email.
    quote_mails = [m for m in capture.outbox[before_quote:] if m.to == "jan@acme.cz"]
    assert len(quote_mails) == 1
    assert "Nacenění" in quote_mails[0].subject

    async with sm() as session:
        o = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
    assert o.status == OrderStatus.QUOTED
    assert o.quoted_total is not None

    # -------- Step 15: Jan leaves a comment + confirms ---------------------
    await _logout(tenant_client)
    await _login(tenant_client, "jan@acme.cz", "janpass99")

    await tenant_client.post(
        f"/app/orders/{order_id}/comments",
        data={"body": "Souhlasím s cenou, pokračujte prosím."},
        follow_redirects=False,
    )
    before_confirm = len(capture.outbox)
    confirm = await tenant_client.post(
        f"/app/orders/{order_id}/transitions/confirmed",
        follow_redirects=False,
    )
    assert confirm.status_code == 303
    confirm_mails = [m for m in capture.outbox[before_confirm:] if m.to == "jan@acme.cz"]
    # The status email goes to contact(s); Jan receives it too since they're
    # an active contact of ACME.
    assert any("Potvrzeno" in m.subject for m in confirm_mails)

    # -------- Step 16: owner walks IN_PRODUCTION → READY → DELIVERED -----
    await _logout(tenant_client)
    await _login(tenant_client, "owner@4mex.cz", "demo1234")
    for status in ("in_production", "ready", "delivered"):
        r = await tenant_client.post(
            f"/app/orders/{order_id}/transitions/{status}",
            follow_redirects=False,
        )
        assert r.status_code == 303, f"{status}: {r.text}"

    async with sm() as session:
        o = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
    assert o.status == OrderStatus.DELIVERED

    # -------- Step 17: audit trail on the order detail page --------------
    detail = await tenant_client.get(f"/app/orders/{order_id}")
    for label in ("Odesláno", "Nacenění", "Potvrzeno", "Ve výrobě", "Připraveno", "Dodáno"):
        assert label in detail.text, f"missing {label} in audit trail"

    # -------- Step 18-19: asset creation + signed movements ---------------
    asset_resp = await tenant_client.post(
        "/app/assets",
        data={
            "customer_id": str(acme_id),
            "code": "AL-2MM",
            "name": "Plech Al 2mm",
            "unit": "kg",
        },
        follow_redirects=False,
    )
    assert asset_resp.status_code == 303
    asset_id = UUID(asset_resp.headers["location"].rsplit("/", 1)[-1])

    await tenant_client.post(
        f"/app/assets/{asset_id}/movements",
        data={"type": "receive", "quantity": "100", "note": "Počáteční dodávka"},
        follow_redirects=False,
    )
    await tenant_client.post(
        f"/app/assets/{asset_id}/movements",
        data={
            "type": "consume",
            "quantity": "25",
            "note": "Spotřeba na Q1",
            "reference_order_id": str(order_id),
        },
        follow_redirects=False,
    )

    async with sm() as session:
        refreshed = (await session.execute(select(Asset).where(Asset.id == asset_id))).scalar_one()
        movements = (
            (await session.execute(select(AssetMovement).where(AssetMovement.asset_id == asset_id)))
            .scalars()
            .all()
        )
    assert refreshed.current_quantity == Decimal("75")
    consume_rows = [m for m in movements if m.reference_order_id is not None]
    assert len(consume_rows) == 1
    assert consume_rows[0].reference_order_id == order_id

    # -------- Step 20: contact sees only its own assets -----------------
    await _logout(tenant_client)
    await _login(tenant_client, "jan@acme.cz", "janpass99")
    assets_page = await tenant_client.get("/app/assets")
    assert assets_page.status_code == 200
    assert "AL-2MM" in assets_page.text

    # Create a second customer + its contact; Jan must NOT see its data.
    async with sm() as session, session.begin():
        other_cust = Customer(
            id=uuid4(), tenant_id=demo_tenant.id, name="Other s.r.o.", ico="99999999"
        )
        session.add(other_cust)
        await session.flush()
        session.add(
            Asset(
                id=uuid4(),
                tenant_id=demo_tenant.id,
                customer_id=other_cust.id,
                code="OTH-X",
                name="Other stuff",
                unit="ks",
                current_quantity=Decimal("10"),
            )
        )

    assets_again = await tenant_client.get("/app/assets")
    assert "OTH-X" not in assets_again.text

    # -------- Step 22: oversize upload rejected -------------------------
    tenant_client._transport.app.state.settings.max_upload_size_mb = 1  # type: ignore[attr-defined]
    await _logout(tenant_client)
    await _login(tenant_client, "jan@acme.cz", "janpass99")
    new_draft = await tenant_client.post(
        "/app/orders", data={"title": "Big file test"}, follow_redirects=False
    )
    new_draft_id = UUID(new_draft.headers["location"].rsplit("/", 1)[-1])
    big_bytes = b"x" * (2 * 1024 * 1024)
    big_upload = await tenant_client.post(
        f"/app/orders/{new_draft_id}/attachments",
        files={"file": ("big.bin", big_bytes, "application/octet-stream")},
        follow_redirects=False,
    )
    assert big_upload.status_code == 413
    # Reset size limit so we don't affect other tests in the same process.
    tenant_client._transport.app.state.settings.max_upload_size_mb = 50  # type: ignore[attr-defined]

    # -------- Step 24: auto_close_delivered_orders after 14 days -------
    async with owner_engine.begin() as conn:
        aged = datetime.now(UTC) - timedelta(days=15)
        await conn.execute(
            text("UPDATE orders SET updated_at = :ts WHERE id = :id"),
            {"ts": aged, "id": order_id},
        )
        # Age comments too — auto-close skips orders with recent comments.
        await conn.execute(
            text("UPDATE order_comments SET created_at = :ts WHERE order_id = :id"),
            {"ts": aged, "id": order_id},
        )

    from app.tasks.periodic import auto_close_delivered_orders

    with freeze_time(datetime.now(UTC)):
        closed = await auto_close_delivered_orders()
    assert closed >= 1

    async with sm() as session:
        o = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
    assert o.status == OrderStatus.CLOSED
    assert o.closed_at is not None


async def test_plan_e2e_cross_tenant_access_is_blocked(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """Step 21: an order belonging to tenant A is invisible to tenant B.

    Uses an ORM-level smoke check rather than spinning up two full
    ASGI clients — we already have tests/test_tenant_isolation.py
    exercising the actual RLS policy at the DB level, this version
    verifies the 404 response shape.
    """
    # Seed a second tenant + user.
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    tenant_b_id = uuid4()
    async with sm() as session, session.begin():
        session.add(
            Tenant(
                id=tenant_b_id,
                slug="other",
                name="Other s.r.o.",
                billing_email="b@other.cz",
                storage_prefix="tenants/other/",
            )
        )
        session.add(
            User(
                id=uuid4(),
                tenant_id=demo_tenant.id,
                email="owner@4mex.cz",
                full_name="Owner",
                role=__import__("app.models.enums", fromlist=["UserRole"]).UserRole.TENANT_ADMIN,
                password_hash=hash_password("demo1234"),
            )
        )
        session.add(
            User(
                id=uuid4(),
                tenant_id=tenant_b_id,
                email="owner@other.cz",
                full_name="Other Owner",
                role=__import__("app.models.enums", fromlist=["UserRole"]).UserRole.TENANT_ADMIN,
                password_hash=hash_password("otherpass"),
            )
        )

    # Create a customer + order in tenant A (4mex).
    await _login(tenant_client, "owner@4mex.cz", "demo1234")
    cust_resp = await tenant_client.post(
        "/app/customers",
        data={"name": "Only4mex", "ico": "11111111", "dic": "", "notes": ""},
        follow_redirects=False,
    )
    cust_id = UUID(cust_resp.headers["location"].rsplit("/", 1)[-1])
    order_resp = await tenant_client.post(
        "/app/orders",
        data={"title": "Tajná zakázka", "customer_id": str(cust_id)},
        follow_redirects=False,
    )
    target_id = UUID(order_resp.headers["location"].rsplit("/", 1)[-1])

    # Switch the tenant header to "other" (the same client instance).
    tenant_client.headers["X-Tenant-Slug"] = "other"
    tenant_client.cookies.clear()
    await _login(tenant_client, "owner@other.cz", "otherpass")

    resp = await tenant_client.get(f"/app/orders/{target_id}")
    assert resp.status_code == 404
