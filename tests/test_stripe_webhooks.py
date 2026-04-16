"""Stripe webhook tests.

Exercises the full ``POST /platform/webhooks/stripe`` flow in **live
mode** (``STRIPE_SECRET_KEY`` set to a fake value). We don't hit the
real Stripe API — signature verification is exercised with a locally
computed HMAC using ``stripe.WebhookSignature.generate_header`` (or a
hand-rolled equivalent for the older SDK), and the upsert / dedup
paths are validated against the DB.

These tests are the "positive + negative" coverage Stripe reviewer C3
asked for.
"""

from __future__ import annotations

import hmac
import json
import time
from collections.abc import AsyncIterator
from hashlib import sha256
from uuid import uuid4

import pytest
from httpx import ASGITransport
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.main import create_app
from app.models.tenant import Tenant
from app.platform.billing.models import Plan, Subscription
from tests.conftest import CsrfAwareClient

pytestmark = pytest.mark.postgres


FAKE_STRIPE_SECRET_KEY = "sk_test_fake_placeholder"
FAKE_WEBHOOK_SECRET = "whsec_test_fake_secret"


def _sign_stripe_event(payload: str, secret: str = FAKE_WEBHOOK_SECRET) -> str:
    """Build a ``Stripe-Signature`` header for ``payload`` with our secret."""
    ts = int(time.time())
    signed_payload = f"{ts}.{payload}"
    signature = hmac.new(
        secret.encode("utf-8"),
        signed_payload.encode("utf-8"),
        sha256,
    ).hexdigest()
    return f"t={ts},v1={signature}"


def _make_event(event_id: str, event_type: str, data_object: dict) -> dict:
    """Produce a Stripe-shaped event envelope.

    The SDK's ``construct_event`` reads ``event.object`` to pick the right
    event class — without it a bare dict causes ``AttributeError``. Every
    real Stripe event carries ``"object": "event"`` so we mirror it here.
    """
    return {
        "id": event_id,
        "object": "event",
        "type": event_type,
        "data": {"object": data_object},
    }


@pytest.fixture
async def stripe_live_client(
    settings, wipe_db, owner_engine
) -> AsyncIterator[tuple[CsrfAwareClient, object]]:
    """Client with Stripe enabled in "live" mode (fake secrets, no network).

    We never actually talk to Stripe because only ``verify_webhook`` is
    exercised and we locally generate the HMAC header.
    """
    settings.feature_platform = True
    settings.stripe_secret_key = FAKE_STRIPE_SECRET_KEY
    settings.stripe_webhook_secret = FAKE_WEBHOOK_SECRET

    async with owner_engine.begin() as conn:
        await conn.execute(text("DELETE FROM platform_tenant_memberships"))
        await conn.execute(text("DELETE FROM platform_identities"))
        await conn.execute(text("DELETE FROM platform_subscriptions"))
        await conn.execute(text("DELETE FROM platform_invoices"))
        await conn.execute(text("DELETE FROM platform_stripe_events"))

    from app.platform.deps import reset_platform_engine

    reset_platform_engine()

    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with CsrfAwareClient(transport=transport, base_url="http://testserver") as ac:
        yield ac, owner_engine

    reset_platform_engine()


async def _seed_tenant_with_trial(owner_engine) -> tuple[Tenant, Subscription]:
    """Insert a tenant + trial subscription directly via the owner engine."""
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        tenant = Tenant(
            id=uuid4(),
            slug="wh-test",
            name="WH Test",
            billing_email="b@wh.cz",
            storage_prefix="tenants/wh-test/",
        )
        session.add(tenant)
        await session.flush()
        starter = (await session.execute(select(Plan).where(Plan.code == "starter"))).scalar_one()
        # Pretend the price IDs are wired for the plan-swap test below.
        starter.stripe_price_id = "price_starter_123"
        pro = (await session.execute(select(Plan).where(Plan.code == "pro"))).scalar_one()
        pro.stripe_price_id = "price_pro_456"
        sub = Subscription(
            tenant_id=tenant.id,
            plan_id=starter.id,
            status="trialing",
        )
        session.add(sub)
        await session.flush()
    async with sm() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.slug == "wh-test"))
        ).scalar_one()
        sub = (
            await session.execute(select(Subscription).where(Subscription.tenant_id == tenant.id))
        ).scalar_one()
    return tenant, sub


async def test_webhook_rejects_missing_or_bad_signature(stripe_live_client) -> None:
    client, _ = stripe_live_client
    payload = json.dumps(_make_event("evt_1", "invoice.paid", {}))
    # No signature header → verify_webhook raises → 400.
    resp = await client.post(
        "/platform/webhooks/stripe",
        content=payload.encode(),
        headers={"stripe-signature": "", "content-type": "application/json"},
    )
    assert resp.status_code == 400

    # Wrong signature → 400.
    resp = await client.post(
        "/platform/webhooks/stripe",
        content=payload.encode(),
        headers={
            "stripe-signature": "t=123,v1=deadbeef",
            "content-type": "application/json",
        },
    )
    assert resp.status_code == 400


async def test_webhook_accepts_valid_signature_and_is_idempotent(
    stripe_live_client,
) -> None:
    client, engine = stripe_live_client
    tenant, _ = await _seed_tenant_with_trial(engine)

    event = _make_event(
        "evt_idempo_1",
        "checkout.session.completed",
        {
            "id": "cs_test_1",
            "customer": "cus_test_1",
            "subscription": "sub_test_1",
            "client_reference_id": str(tenant.id),
            "metadata": {"tenant_id": str(tenant.id)},
        },
    )
    payload = json.dumps(event)
    sig = _sign_stripe_event(payload)

    # First delivery: process.
    resp = await client.post(
        "/platform/webhooks/stripe",
        content=payload.encode(),
        headers={"stripe-signature": sig, "content-type": "application/json"},
    )
    assert resp.status_code == 200, resp.text

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        t = (await session.execute(select(Tenant).where(Tenant.id == tenant.id))).scalar_one()
        assert t.stripe_customer_id == "cus_test_1"
        sub = (
            await session.execute(select(Subscription).where(Subscription.tenant_id == tenant.id))
        ).scalar_one()
        assert sub.stripe_subscription_id == "sub_test_1"

    # Same event_id replayed: still 200 but nothing else happens.
    # We prove "nothing else happens" by changing the customer id in the
    # body — a re-processed handler would overwrite; a deduped one won't.
    replay_event = json.loads(payload)
    replay_event["data"]["object"]["customer"] = "cus_DIFFERENT"
    replay_payload = json.dumps(replay_event)
    replay_sig = _sign_stripe_event(replay_payload)
    resp2 = await client.post(
        "/platform/webhooks/stripe",
        content=replay_payload.encode(),
        headers={"stripe-signature": replay_sig, "content-type": "application/json"},
    )
    assert resp2.status_code == 200

    async with sm() as session:
        t = (await session.execute(select(Tenant).where(Tenant.id == tenant.id))).scalar_one()
        # Still the original customer id — replay was deduped.
        assert t.stripe_customer_id == "cus_test_1"


async def test_subscription_updated_changes_plan_and_period(stripe_live_client) -> None:
    """A customer.subscription.updated event with a new price.id swaps plan."""
    client, engine = stripe_live_client
    tenant, sub = await _seed_tenant_with_trial(engine)

    now_ts = int(time.time())
    event = _make_event(
        "evt_sub_upd_1",
        "customer.subscription.updated",
        {
            "id": "sub_1",
            "customer": "cus_1",
            "status": "active",
            "current_period_start": now_ts,
            "current_period_end": now_ts + 30 * 86400,
            "trial_end": None,
            "cancel_at_period_end": False,
            "metadata": {"tenant_id": str(tenant.id)},
            "items": {"data": [{"price": {"id": "price_pro_456"}}]},
        },
    )
    payload = json.dumps(event)
    sig = _sign_stripe_event(payload)
    resp = await client.post(
        "/platform/webhooks/stripe",
        content=payload.encode(),
        headers={"stripe-signature": sig, "content-type": "application/json"},
    )
    assert resp.status_code == 200

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        sub_reloaded = (
            await session.execute(select(Subscription).where(Subscription.id == sub.id))
        ).scalar_one()
        assert sub_reloaded.status == "active"
        plan = (
            await session.execute(select(Plan).where(Plan.id == sub_reloaded.plan_id))
        ).scalar_one()
        assert plan.code == "pro"
        assert sub_reloaded.current_period_start is not None
        assert sub_reloaded.current_period_end is not None


async def test_invoice_paid_records_invoice(stripe_live_client) -> None:
    client, engine = stripe_live_client
    tenant, _ = await _seed_tenant_with_trial(engine)

    event = _make_event(
        "evt_inv_1",
        "invoice.paid",
        {
            "id": "in_1",
            "number": "INV-0001",
            "amount_paid": 49000,
            "currency": "czk",
            "metadata": {"tenant_id": str(tenant.id)},
            "hosted_invoice_url": "https://stripe/invoice/1",
            "invoice_pdf": "https://stripe/invoice/1.pdf",
        },
    )
    payload = json.dumps(event)
    sig = _sign_stripe_event(payload)
    resp = await client.post(
        "/platform/webhooks/stripe",
        content=payload.encode(),
        headers={"stripe-signature": sig, "content-type": "application/json"},
    )
    assert resp.status_code == 200

    from app.platform.billing.models import Invoice

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        inv = (
            await session.execute(select(Invoice).where(Invoice.stripe_invoice_id == "in_1"))
        ).scalar_one()
        assert inv.number == "INV-0001"
        assert inv.amount_cents == 49000
        assert inv.currency == "CZK"
        assert inv.status == "paid"


async def test_invoice_payment_failed_marks_past_due(stripe_live_client) -> None:
    client, engine = stripe_live_client
    tenant, sub = await _seed_tenant_with_trial(engine)

    event = _make_event(
        "evt_inv_fail_1",
        "invoice.payment_failed",
        {"id": "in_2", "metadata": {"tenant_id": str(tenant.id)}},
    )
    payload = json.dumps(event)
    sig = _sign_stripe_event(payload)
    resp = await client.post(
        "/platform/webhooks/stripe",
        content=payload.encode(),
        headers={"stripe-signature": sig, "content-type": "application/json"},
    )
    assert resp.status_code == 200

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        sub_reloaded = (
            await session.execute(select(Subscription).where(Subscription.id == sub.id))
        ).scalar_one()
        assert sub_reloaded.status == "past_due"


async def test_subscription_deleted_downgrades_to_community(stripe_live_client) -> None:
    client, engine = stripe_live_client
    tenant, sub = await _seed_tenant_with_trial(engine)

    event = _make_event(
        "evt_sub_del_1",
        "customer.subscription.deleted",
        {"id": "sub_1", "metadata": {"tenant_id": str(tenant.id)}},
    )
    payload = json.dumps(event)
    sig = _sign_stripe_event(payload)
    resp = await client.post(
        "/platform/webhooks/stripe",
        content=payload.encode(),
        headers={"stripe-signature": sig, "content-type": "application/json"},
    )
    assert resp.status_code == 200

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        sub_reloaded = (
            await session.execute(select(Subscription).where(Subscription.id == sub.id))
        ).scalar_one()
        assert sub_reloaded.status == "canceled"
        plan = (
            await session.execute(select(Plan).where(Plan.id == sub_reloaded.plan_id))
        ).scalar_one()
        assert plan.code == "community"


async def test_webhook_rejects_tenant_spoof_via_metadata(stripe_live_client, owner_engine) -> None:
    """Round-2 audit P0-1: a paying tenant A must NOT be able to put
    tenant B's UUID in their own Stripe customer's metadata and thereby
    hijack our webhook effects onto tenant B. ``_resolve_tenant_id``
    cross-checks the ``customer`` id against ``Tenant.stripe_customer_id``
    and refuses to resolve on mismatch."""
    client, engine = stripe_live_client

    # Set up two tenants. Tenant A has a Stripe customer id on file;
    # tenant B is the would-be victim.
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        tenant_a = Tenant(
            id=uuid4(),
            slug="spoof-a",
            name="Tenant A",
            billing_email="a@spoof.cz",
            storage_prefix="tenants/spoof-a/",
            stripe_customer_id="cus_A_live",
        )
        tenant_b = Tenant(
            id=uuid4(),
            slug="spoof-b",
            name="Tenant B victim",
            billing_email="b@spoof.cz",
            storage_prefix="tenants/spoof-b/",
        )
        session.add(tenant_a)
        session.add(tenant_b)
        await session.flush()
        starter = (await session.execute(select(Plan).where(Plan.code == "starter"))).scalar_one()
        starter.stripe_price_id = "price_spoof_starter"
        session.add(Subscription(tenant_id=tenant_a.id, plan_id=starter.id, status="trialing"))
        session.add(Subscription(tenant_id=tenant_b.id, plan_id=starter.id, status="active"))

    # Attack payload: "customer" = tenant A's customer, "metadata.tenant_id"
    # claims to be tenant B. Should be refused.
    event = _make_event(
        "evt_spoof_1",
        "customer.subscription.deleted",
        {
            "id": "sub_spoof",
            "customer": "cus_A_live",
            "metadata": {"tenant_id": str(tenant_b.id)},
        },
    )
    payload = json.dumps(event)
    sig = _sign_stripe_event(payload)
    resp = await client.post(
        "/platform/webhooks/stripe",
        content=payload.encode(),
        headers={"stripe-signature": sig, "content-type": "application/json"},
    )
    # Handler raises WebhookNotYetReady → router returns 503 → Stripe
    # retries; but the DB state is NOT mutated.
    assert resp.status_code == 503

    async with sm() as session:
        sub_b = (
            await session.execute(select(Subscription).where(Subscription.tenant_id == tenant_b.id))
        ).scalar_one()
        assert sub_b.status == "active", "victim subscription must NOT be cancelled"


async def test_webhook_no_tenant_rolls_back_dedup(stripe_live_client, owner_engine) -> None:
    """When a handler raises WebhookNotYetReady, the router rolls back
    the transaction (dedup row + any partial writes) so Stripe retries.
    Regression test for the round-2 P0 that the dedup INSERT previously
    committed silently on no-op returns."""
    from sqlalchemy import text

    client, engine = stripe_live_client

    # Unknown tenant → _resolve_tenant_id returns None → handler raises
    event = _make_event(
        "evt_notready_1",
        "customer.subscription.updated",
        {
            "id": "sub_unknown",
            "customer": "cus_never_seen",
            "status": "active",
        },
    )
    payload = json.dumps(event)
    sig = _sign_stripe_event(payload)
    resp = await client.post(
        "/platform/webhooks/stripe",
        content=payload.encode(),
        headers={"stripe-signature": sig, "content-type": "application/json"},
    )
    assert resp.status_code == 503

    # Dedup row must also be absent so a retry can still process the event.
    async with engine.begin() as conn:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM platform_stripe_events WHERE id = 'evt_notready_1'")
            )
        ).scalar_one()
    assert count == 0, "dedup row must have rolled back with the failed handler"


async def test_subscription_updated_scans_all_items_for_plan(stripe_live_client) -> None:
    """Round-2 S-N4: a multi-line subscription (setup fee + recurring
    plan) must still swap to the right plan. Previously items[0] would
    have been taken blindly — if Stripe put the setup fee first we'd
    have missed the Pro price_id entirely."""
    client, engine = stripe_live_client
    tenant, sub = await _seed_tenant_with_trial(engine)

    now_ts = int(time.time())
    event = _make_event(
        "evt_sub_multi_1",
        "customer.subscription.updated",
        {
            "id": "sub_multi",
            "customer": "cus_multi",
            "status": "active",
            "current_period_start": now_ts,
            "current_period_end": now_ts + 30 * 86400,
            "metadata": {"tenant_id": str(tenant.id)},
            # Setup fee comes FIRST; real plan second.
            "items": {
                "data": [
                    {"price": {"id": "price_setup_fee_one_off"}},
                    {"price": {"id": "price_pro_456"}},
                ]
            },
        },
    )
    # Tenant has no stripe_customer_id, so this event comes in via
    # metadata. Seed the customer id so the authoritative customer
    # lookup matches the metadata claim (no spoofing block).
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        t = (await session.execute(select(Tenant).where(Tenant.id == tenant.id))).scalar_one()
        t.stripe_customer_id = "cus_multi"

    payload = json.dumps(event)
    sig = _sign_stripe_event(payload)
    resp = await client.post(
        "/platform/webhooks/stripe",
        content=payload.encode(),
        headers={"stripe-signature": sig, "content-type": "application/json"},
    )
    assert resp.status_code == 200

    async with sm() as session:
        sub_reloaded = (
            await session.execute(select(Subscription).where(Subscription.id == sub.id))
        ).scalar_one()
        plan = (
            await session.execute(select(Plan).where(Plan.id == sub_reloaded.plan_id))
        ).scalar_one()
        # Pro was in items[1] but the scan must still find it.
        assert plan.code == "pro"


async def test_unknown_event_type_is_no_op(stripe_live_client) -> None:
    client, _ = stripe_live_client

    event = _make_event("evt_unknown_1", "customer.created", {})
    payload = json.dumps(event)
    sig = _sign_stripe_event(payload)
    resp = await client.post(
        "/platform/webhooks/stripe",
        content=payload.encode(),
        headers={"stripe-signature": sig, "content-type": "application/json"},
    )
    # Unknown events still return 200 (Stripe will retry otherwise).
    assert resp.status_code == 200
