"""Regression tests for the 2026-10-03 audit — billing (themes T3, T4).

See docs/audit-runs/2026-10-03-full/findings.md. Stripe is never
contacted: every SDK call is patched.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from httpx import ASGITransport
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.email.sender import CaptureSender
from app.main import create_app
from app.models.tenant import Tenant
from app.platform.billing.models import Invoice, Plan, Subscription
from app.platform.billing.webhooks import (
    handle_charge_refunded,
    handle_checkout_completed,
    handle_invoice_paid,
    handle_invoice_payment_failed,
    handle_subscription_deleted,
    handle_subscription_upserted,
)
from tests.conftest import CsrfAwareClient

pytestmark = pytest.mark.postgres

PASSWORD = "correct-horse-battery-staple"


# ------------------------------------------------------------------ fixtures


@pytest.fixture
async def billing_app(settings, wipe_db, owner_engine) -> AsyncIterator[CsrfAwareClient]:
    """Platform client; Stripe OFF unless a test flips it."""
    settings.feature_platform = True
    settings.stripe_secret_key = ""
    settings.stripe_webhook_secret = ""
    from app.platform.deps import reset_platform_engine

    reset_platform_engine()
    app = create_app(settings)
    app.state.email_sender = CaptureSender()
    async with CsrfAwareClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac
    reset_platform_engine()


async def _signup(client, owner_engine, *, slug: str, email: str, plan: str | None = None):
    data = {
        "company_name": f"{slug} s.r.o.",
        "slug": slug,
        "owner_email": email,
        "owner_full_name": "Owner",
        "password": PASSWORD,
        "terms_accepted": "1",
    }
    if plan:
        data["plan"] = plan
    resp = await client.post("/platform/signup", data=data, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE platform_identities SET email_verified_at = now() WHERE email = :e"),
            {"e": email},
        )


async def _sub_for(owner_engine, slug: str) -> tuple[Tenant, Subscription, Plan]:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s:
        tenant = (await s.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one()
        sub = (
            await s.execute(select(Subscription).where(Subscription.tenant_id == tenant.id))
        ).scalar_one()
        plan = (await s.execute(select(Plan).where(Plan.id == sub.plan_id))).scalar_one()
    return tenant, sub, plan


async def _update_sub(owner_engine, tenant_id, **values) -> None:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s, s.begin():
        sub = (
            await s.execute(select(Subscription).where(Subscription.tenant_id == tenant_id))
        ).scalar_one()
        for k, v in values.items():
            setattr(sub, k, v)


# ---------------------------------------------------- D2 / BIZ-01 guard


def test_demo_checkout_defaults_off_only_in_production() -> None:
    assert Settings(APP_ENV="development").billing_demo_checkout_allowed is True
    assert Settings(APP_ENV="test").billing_demo_checkout_allowed is True
    assert Settings(APP_ENV="production").billing_demo_checkout_allowed is False
    # Explicit operator override for a staging box.
    assert (
        Settings(
            APP_ENV="production", BILLING_DEMO_MODE_ALLOWED="true"
        ).billing_demo_checkout_allowed
        is True
    )


async def test_production_without_stripe_never_grants_a_plan(
    billing_app, owner_engine, settings
) -> None:
    """BIZ-01: "Upgrade" in prod demo mode flipped the plan for free and
    flashed success. Now: no plan change, no success, the bank-transfer
    message with the operator address."""
    settings.billing_demo_mode_allowed = False
    await _signup(billing_app, owner_engine, slug="d2co", email="o@d2co.cz")

    resp = await billing_app.post("/platform/billing/checkout/pro", follow_redirects=False)
    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert "checkout=offline" in loc and "success" not in loc

    _, sub, plan = await _sub_for(owner_engine, "d2co")
    assert plan.code == "starter"
    assert sub.status == "trialing"

    page = await billing_app.get(loc)
    assert page.status_code == 200
    assert "Online payment is being set up" in page.text
    assert settings.platform_operator_email in page.text
    assert "Demo mode (Stripe not configured)" not in page.text


async def test_post_verify_checkout_also_refuses_in_production(
    billing_app, owner_engine, settings
) -> None:
    settings.billing_demo_mode_allowed = False
    await _signup(billing_app, owner_engine, slug="d2pv", email="o@d2pv.cz", plan="pro")
    _, before, _ = await _sub_for(owner_engine, "d2pv")

    resp = await billing_app.post(
        "/platform/billing/post-verify-checkout/pro", follow_redirects=False
    )
    assert resp.status_code == 303
    assert "checkout=offline" in resp.headers["location"]
    _, sub, _ = await _sub_for(owner_engine, "d2pv")
    assert sub.status == before.status == "trialing"


async def test_enterprise_is_never_sold_through_checkout(billing_app, owner_engine) -> None:
    await _signup(billing_app, owner_engine, slug="entco", email="o@entco.cz")
    resp = await billing_app.post("/platform/billing/checkout/enterprise", follow_redirects=False)
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    _, _, plan = await _sub_for(owner_engine, "entco")
    assert plan.code == "starter"


# ---------------------------------------- BIZ-02 / UX-01 conversion path


async def test_trialist_gets_a_continue_on_current_plan_button(billing_app, owner_engine) -> None:
    await _signup(billing_app, owner_engine, slug="contco", email="o@contco.cz")
    resp = await billing_app.get("/platform/billing")
    assert resp.status_code == 200
    assert 'action="/platform/billing/checkout/starter"' in resp.text
    assert "Continue on" in resp.text or "Pokračovat" in resp.text


async def test_verify_email_primary_cta_goes_to_the_app_not_checkout(
    billing_app, owner_engine, settings
) -> None:
    """UX-01: "30 days free, no card" — the main button after verifying
    must not lead into billing details / Stripe Checkout."""
    from app.platform.models import Identity
    from app.platform.routers.signup import _build_verify_url

    resp = await billing_app.post(
        "/platform/signup",
        data={
            "company_name": "Verify Co",
            "slug": "verifyco",
            "owner_email": "o@verifyco.cz",
            "owner_full_name": "O",
            "password": PASSWORD,
            "terms_accepted": "1",
            "plan": "pro",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s:
        identity = (
            await s.execute(select(Identity).where(Identity.email == "o@verifyco.cz"))
        ).scalar_one()
    url = _build_verify_url(settings, identity.id)
    page = await billing_app.get(url.replace(settings.app_base_url.rstrip("/"), ""))
    assert page.status_code == 200
    html = page.text
    assert "post-verify-checkout" not in html
    primary = html.index("/platform/select-tenant")
    assert primary < html.index("/platform/billing")


# ------------------------------------------------------- SEC-1 webhook secret


async def test_webhook_refused_when_webhook_secret_empty(billing_app, settings) -> None:
    """A payload HMAC'd with the empty key used to verify fine."""
    import hmac
    import time
    from hashlib import sha256

    settings.stripe_secret_key = "sk_test_fake"
    settings.stripe_webhook_secret = ""
    payload = json.dumps(
        {"id": "evt_forged", "object": "event", "type": "invoice.paid", "data": {"object": {}}}
    )
    ts = int(time.time())
    sig = hmac.new(b"", f"{ts}.{payload}".encode(), sha256).hexdigest()
    resp = await billing_app.post(
        "/platform/webhooks/stripe",
        content=payload.encode(),
        headers={"stripe-signature": f"t={ts},v1={sig}", "content-type": "application/json"},
    )
    assert resp.status_code == 400


def test_production_refuses_to_boot_with_stripe_key_but_no_webhook_secret() -> None:
    s = Settings(
        APP_ENV="production",
        APP_SECRET_KEY="x" * 48,
        STRIPE_SECRET_KEY="sk_live_x",
        STRIPE_WEBHOOK_SECRET="",
    )
    with pytest.raises(RuntimeError, match="STRIPE_WEBHOOK_SECRET"):
        create_app(s)


# ---------------------------------------------- Codex-1 plan change = modify


def _fake_stripe_sub(price_id: str) -> dict:
    return {
        "id": "sub_live_1",
        "items": {"data": [{"id": "si_1", "price": {"id": price_id}}]},
    }


async def _live_tenant(billing_app, owner_engine, settings, slug: str):
    settings.stripe_secret_key = "sk_test_fake"
    await _signup(billing_app, owner_engine, slug=slug, email=f"o@{slug}.cz")
    tenant, _, _ = await _sub_for(owner_engine, slug)
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE platform_plans SET stripe_price_id = 'price_starter' WHERE code='starter'")
        )
        await conn.execute(
            text("UPDATE platform_plans SET stripe_price_id = 'price_pro' WHERE code='pro'")
        )
    return tenant


async def test_plan_change_modifies_the_existing_subscription(
    billing_app, owner_engine, settings
) -> None:
    tenant = await _live_tenant(billing_app, owner_engine, settings, "modco")
    await _update_sub(owner_engine, tenant.id, status="active", stripe_subscription_id="sub_live_1")
    with (
        patch("stripe.Subscription.retrieve", return_value=_fake_stripe_sub("price_starter")),
        patch("stripe.Subscription.modify") as modify,
        patch("stripe.checkout.Session.create") as create,
    ):
        resp = await billing_app.post("/platform/billing/checkout/pro", follow_redirects=False)
    assert resp.status_code == 303
    create.assert_not_called()
    modify.assert_called_once()
    args, kwargs = modify.call_args
    assert args[0] == "sub_live_1"
    assert kwargs["items"] == [{"id": "si_1", "price": "price_pro"}]
    assert kwargs["proration_behavior"] == "create_prorations"
    _, _, plan = await _sub_for(owner_engine, "modco")
    assert plan.code == "pro"


async def test_checkout_reuses_open_session_and_expires_other_plan(
    billing_app, owner_engine, settings
) -> None:
    """One open Checkout session per tenant — two paid checkouts used to
    mean two subscriptions."""
    tenant = await _live_tenant(billing_app, owner_engine, settings, "serco")
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE tenants SET settings = :s WHERE id = :id"),
            {
                "s": json.dumps(
                    {"billing_ico": "12345678", "billing_name": "S", "billing_address": "A"}
                ),
                "id": tenant.id,
            },
        )

    created = SimpleNamespace(id="cs_1", url="https://checkout.stripe.test/cs_1")
    with patch("stripe.checkout.Session.create", return_value=created) as create:
        resp = await billing_app.post("/platform/billing/checkout/starter", follow_redirects=False)
    assert resp.headers["location"] == created.url
    assert create.call_count == 1
    _, sub, _ = await _sub_for(owner_engine, "serco")
    assert sub.pending_checkout_session_id == "cs_1"

    # Same plan again while that session is open → same page, no new session.
    open_same = {
        "id": "cs_1",
        "status": "open",
        "url": created.url,
        "metadata": {"plan_code": "starter"},
    }
    with (
        patch("stripe.checkout.Session.retrieve", return_value=open_same),
        patch("stripe.checkout.Session.create") as create2,
    ):
        resp = await billing_app.post("/platform/billing/checkout/starter", follow_redirects=False)
    assert resp.headers["location"] == created.url
    create2.assert_not_called()

    # Another plan → the open session is expired first, then a new one.
    created2 = SimpleNamespace(id="cs_2", url="https://checkout.stripe.test/cs_2")
    with (
        patch("stripe.checkout.Session.retrieve", return_value=open_same),
        patch("stripe.checkout.Session.expire") as expire,
        patch("stripe.checkout.Session.create", return_value=created2) as create3,
    ):
        resp = await billing_app.post("/platform/billing/checkout/pro", follow_redirects=False)
    expire.assert_called_once_with("cs_1")
    assert resp.headers["location"] == created2.url
    assert create3.call_args.kwargs["idempotency_key"].endswith(":after-cs_1")

    # A completed session whose webhook is still in flight → refuse.
    done = {"id": "cs_2", "status": "complete", "subscription": "sub_new", "metadata": {}}
    with (
        patch("stripe.checkout.Session.retrieve", return_value=done),
        patch("stripe.checkout.Session.create") as create4,
    ):
        resp = await billing_app.post("/platform/billing/checkout/pro", follow_redirects=False)
    create4.assert_not_called()
    assert "notice=" in resp.headers["location"]


async def test_checkout_failure_is_a_flash_not_a_500(billing_app, owner_engine, settings) -> None:
    tenant = await _live_tenant(billing_app, owner_engine, settings, "failco")
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE tenants SET settings = :s WHERE id = :id"),
            {
                "s": json.dumps(
                    {"billing_ico": "12345678", "billing_name": "S", "billing_address": "A"}
                ),
                "id": tenant.id,
            },
        )
    with patch("stripe.checkout.Session.create", side_effect=RuntimeError("boom")):
        resp = await billing_app.post("/platform/billing/checkout/pro", follow_redirects=False)
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]


# ------------------------------------------------------ Codex-2 / LOGIC-23


async def test_disabled_admin_loses_billing_authority(billing_app, owner_engine) -> None:
    await _signup(billing_app, owner_engine, slug="disco", email="o@disco.cz")
    async with owner_engine.begin() as conn:
        await conn.execute(text("UPDATE users SET is_active = false WHERE email = 'o@disco.cz'"))
    resp = await billing_app.get("/platform/billing")
    assert resp.status_code == 404
    resp = await billing_app.post("/platform/billing/cancel-subscription", follow_redirects=False)
    assert resp.status_code == 404


async def test_owner_of_two_tenants_can_bill_each(billing_app, owner_engine) -> None:
    from app.platform.models import Identity, TenantMembership
    from app.platform.service import create_tenant_with_owner

    await _signup(billing_app, owner_engine, slug="first", email="o@two.cz")
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s, s.begin():
        tenant2, _owner2 = await create_tenant_with_owner(
            s,
            slug="second",
            name="Second",
            owner_email="o@two.cz",
            owner_full_name="Owner",
            owner_password=PASSWORD,
        )
        from app.platform.billing.service import start_trial_subscription

        await start_trial_subscription(s, tenant=tenant2, plan_code="pro")
        identity = (
            await s.execute(select(Identity).where(Identity.email == "o@two.cz"))
        ).scalar_one()
        assert (
            (
                await s.execute(
                    select(TenantMembership).where(TenantMembership.identity_id == identity.id)
                )
            )
            .scalars()
            .all()
        )

    page = await billing_app.get("/platform/billing?tenant=second")
    assert page.status_code == 200
    assert "Second" in page.text
    assert "?tenant=second" in page.text  # forms carry the choice

    resp = await billing_app.post(
        "/platform/billing/cancel-subscription?tenant=second", follow_redirects=False
    )
    assert resp.status_code == 303
    assert "tenant=second" in resp.headers["location"]
    _, sub2, _ = await _sub_for(owner_engine, "second")
    _, sub1, _ = await _sub_for(owner_engine, "first")
    assert sub2.status == "canceled"
    assert sub1.status == "trialing"

    # A tenant the identity cannot bill is a 404, never a silent fallback.
    assert (await billing_app.get("/platform/billing?tenant=nope")).status_code == 404


# ----------------------------------------------------------- Codex-7 cancel


async def test_repeated_cancellation_never_extends_access(billing_app, owner_engine) -> None:
    await _signup(billing_app, owner_engine, slug="cancelco", email="o@cancelco.cz")
    tenant, _, _ = await _sub_for(owner_engine, "cancelco")
    past = datetime.now(UTC) - timedelta(days=2)
    await _update_sub(owner_engine, tenant.id, current_period_end=past, trial_ends_at=past)

    await billing_app.post("/platform/billing/cancel-subscription", follow_redirects=False)
    _, first, _ = await _sub_for(owner_engine, "cancelco")
    assert first.status == "canceled"

    resp = await billing_app.post("/platform/billing/cancel-subscription", follow_redirects=False)
    assert resp.status_code == 303
    _, second, _ = await _sub_for(owner_engine, "cancelco")
    assert second.current_period_end == first.current_period_end
    assert second.canceled_at == first.canceled_at


# ------------------------------------------------ webhook state machine


async def _seed(
    owner_engine, *, status: str, sub_id: str | None, tenant_active: bool = True, **extra
):
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s, s.begin():
        tenant = Tenant(
            id=uuid4(),
            slug=f"wh-{uuid4().hex[:8]}",
            name="WH",
            billing_email=f"wh-{uuid4().hex[:6]}@example.com",
            storage_prefix=f"wh-{uuid4().hex[:8]}/",
            is_active=tenant_active,
        )
        s.add(tenant)
        await s.flush()
        starter = (await s.execute(select(Plan).where(Plan.code == "starter"))).scalar_one()
        sub = Subscription(
            tenant_id=tenant.id,
            plan_id=starter.id,
            status=status,
            stripe_subscription_id=sub_id,
            current_period_end=datetime.now(UTC) + timedelta(days=10),
            **extra,
        )
        s.add(sub)
    return tenant


async def _apply(owner_engine, handler, event: dict) -> None:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s, s.begin():
        await handler(s, event)


async def _state(owner_engine, tenant_id) -> tuple[Subscription, Tenant]:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s:
        sub = (
            await s.execute(select(Subscription).where(Subscription.tenant_id == tenant_id))
        ).scalar_one()
        tenant = (await s.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()
    return sub, tenant


def _ev(etype: str, obj: dict, tenant_id, created: int | None = None) -> dict:
    obj = {"metadata": {"tenant_id": str(tenant_id)}, **obj}
    ev = {"id": f"evt_{uuid4().hex[:10]}", "type": etype, "data": {"object": obj}}
    if created is not None:
        ev["created"] = created
    return ev


async def test_codex3_checkout_for_new_subscription_unlocks_canceled_tenant(owner_engine, wipe_db):
    tenant = await _seed(owner_engine, status="canceled", sub_id="sub_old", tenant_active=False)
    await _apply(
        owner_engine,
        handle_checkout_completed,
        _ev(
            "checkout.session.completed",
            {
                "id": "cs_x",
                "subscription": "sub_new",
                "payment_status": "paid",
                "status": "complete",
            },
            tenant.id,
        ),
    )
    sub, t = await _state(owner_engine, tenant.id)
    assert sub.stripe_subscription_id == "sub_new"
    assert sub.status == "active"
    assert t.is_active is True
    # …and the new subscription's own updates are no longer "stale".
    await _apply(
        owner_engine,
        handle_subscription_upserted,
        _ev("customer.subscription.updated", {"id": "sub_new", "status": "past_due"}, tenant.id),
    )
    sub, _ = await _state(owner_engine, tenant.id)
    assert sub.status == "past_due"


async def test_codex5_events_for_superseded_subscription_are_ignored(owner_engine, wipe_db):
    now = int(datetime.now(UTC).timestamp())
    tenant = await _seed(
        owner_engine,
        status="active",
        sub_id="sub_new",
        stripe_subscription_created_at=datetime.fromtimestamp(now, tz=UTC),
    )
    # Late deletion of the old subscription must not cancel the new one.
    await _apply(
        owner_engine,
        handle_subscription_deleted,
        _ev("customer.subscription.deleted", {"id": "sub_old"}, tenant.id),
    )
    # Late update of the old one must not replace id / status.
    await _apply(
        owner_engine,
        handle_subscription_upserted,
        _ev(
            "customer.subscription.updated",
            {"id": "sub_old", "status": "canceled", "created": now - 86400},
            tenant.id,
        ),
    )
    sub, _ = await _state(owner_engine, tenant.id)
    assert sub.stripe_subscription_id == "sub_new"
    assert sub.status == "active"


async def test_out_of_order_update_for_same_subscription_is_ignored(owner_engine, wipe_db):
    now = int(datetime.now(UTC).timestamp())
    tenant = await _seed(owner_engine, status="active", sub_id="sub_1")
    await _apply(
        owner_engine,
        handle_subscription_upserted,
        _ev("customer.subscription.updated", {"id": "sub_1", "status": "past_due"}, tenant.id, now),
    )
    await _apply(
        owner_engine,
        handle_subscription_upserted,
        _ev(
            "customer.subscription.updated",
            {"id": "sub_1", "status": "active"},
            tenant.id,
            now - 60,
        ),
    )
    sub, _ = await _state(owner_engine, tenant.id)
    assert sub.status == "past_due"


async def test_codex4_late_payment_failure_does_not_revive_canceled(owner_engine, wipe_db):
    tenant = await _seed(owner_engine, status="canceled", sub_id="sub_1")
    await _apply(
        owner_engine,
        handle_invoice_payment_failed,
        _ev("invoice.payment_failed", {"id": "in_1", "subscription": "sub_1"}, tenant.id),
    )
    sub, _ = await _state(owner_engine, tenant.id)
    assert sub.status == "canceled"


async def test_codex4_payment_failure_must_match_current_subscription(owner_engine, wipe_db):
    tenant = await _seed(owner_engine, status="active", sub_id="sub_1")
    await _apply(
        owner_engine,
        handle_invoice_payment_failed,
        _ev("invoice.payment_failed", {"id": "in_1", "subscription": "sub_other"}, tenant.id),
    )
    sub, _ = await _state(owner_engine, tenant.id)
    assert sub.status == "active"
    # New ("basil") API shape: parent.subscription_details.subscription.
    await _apply(
        owner_engine,
        handle_invoice_payment_failed,
        _ev(
            "invoice.payment_failed",
            {"id": "in_2", "parent": {"subscription_details": {"subscription": "sub_1"}}},
            tenant.id,
        ),
    )
    sub, _ = await _state(owner_engine, tenant.id)
    assert sub.status == "past_due"
    assert sub.status_changed_at is not None


async def test_codex6_stripe_update_does_not_lift_operator_suspension(owner_engine, wipe_db):
    tenant = await _seed(
        owner_engine,
        status="active",
        sub_id="sub_1",
        tenant_active=False,
        operator_suspended_at=datetime.now(UTC),
    )
    await _apply(
        owner_engine,
        handle_subscription_upserted,
        _ev(
            "customer.subscription.updated",
            {"id": "sub_1", "status": "active", "cancel_at_period_end": True},
            tenant.id,
        ),
    )
    _, t = await _state(owner_engine, tenant.id)
    assert t.is_active is False


async def test_codex12_refund_before_invoice_paid_is_kept(owner_engine, wipe_db):
    tenant = await _seed(owner_engine, status="active", sub_id="sub_1")
    await _apply(
        owner_engine,
        handle_charge_refunded,
        _ev(
            "charge.refunded",
            {"id": "ch_1", "invoice": "in_r", "amount": 49000, "amount_refunded": 49000},
            tenant.id,
        ),
    )
    await _apply(
        owner_engine,
        handle_invoice_paid,
        _ev(
            "invoice.paid",
            {"id": "in_r", "number": "INV-9", "amount_paid": 49000, "currency": "czk"},
            tenant.id,
        ),
    )
    # A stale partial-refund event must not downgrade the full refund.
    await _apply(
        owner_engine,
        handle_charge_refunded,
        _ev(
            "charge.refunded",
            {"id": "ch_1", "invoice": "in_r", "amount": 49000, "amount_refunded": 1000},
            tenant.id,
        ),
    )
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s:
        inv = (
            await s.execute(select(Invoice).where(Invoice.stripe_invoice_id == "in_r"))
        ).scalar_one()
    assert inv.status == "refunded"
    assert inv.number == "INV-9"
    assert inv.paid_at is not None


async def test_deleted_unpaid_subscription_does_not_grant_the_unpaid_period(owner_engine, wipe_db):
    tenant = await _seed(owner_engine, status="unpaid", sub_id="sub_1")
    now = int(datetime.now(UTC).timestamp())
    await _apply(
        owner_engine,
        handle_subscription_deleted,
        _ev(
            "customer.subscription.deleted",
            {"id": "sub_1", "current_period_end": now + 20 * 86400, "ended_at": now},
            tenant.id,
        ),
    )
    sub, _ = await _state(owner_engine, tenant.id)
    assert sub.status == "canceled"
    assert abs(sub.current_period_end.timestamp() - now) < 2


def test_cancel_with_stripe_uses_magicmock_shape() -> None:
    """``stripe_get`` copes with StripeObject-like objects whose ``items``
    attribute is the dict method."""
    from app.platform.billing.service import stripe_get, stripe_period_value

    obj = MagicMock()
    obj.__getitem__.side_effect = lambda k: {"items": {"data": [{"current_period_end": 5}]}}[k]
    assert stripe_period_value(obj, "current_period_end") == 5
    assert stripe_get({"a": 1}, "a") == 1
