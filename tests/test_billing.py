"""Billing tests — demo mode (no real Stripe API calls)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.email.sender import CaptureSender
from app.main import create_app
from app.models.tenant import Tenant
from app.platform.billing.models import Plan, Subscription
from tests.conftest import CsrfAwareClient

pytestmark = pytest.mark.postgres


@pytest.fixture
async def billing_client(
    settings, wipe_db, owner_engine
) -> AsyncIterator[tuple[CsrfAwareClient, CaptureSender]]:
    settings.feature_platform = True
    # Demo mode: no stripe_secret_key.
    settings.stripe_secret_key = ""

    async with owner_engine.begin() as conn:
        await conn.execute(text("DELETE FROM platform_tenant_memberships"))
        await conn.execute(text("DELETE FROM platform_identities"))
        # Subscriptions + invoices are tenant-FK'd so the wipe_db tenants
        # wipe cascades, but be explicit for paranoia.
        await conn.execute(text("DELETE FROM platform_subscriptions"))
        await conn.execute(text("DELETE FROM platform_invoices"))

    from app.platform.deps import reset_platform_engine

    reset_platform_engine()

    app = create_app(settings)
    sender = CaptureSender()
    app.state.email_sender = sender
    transport = ASGITransport(app=app)
    async with CsrfAwareClient(transport=transport, base_url="http://testserver") as ac:
        yield ac, sender

    reset_platform_engine()


async def _mark_verified(owner_engine, email: str) -> None:
    """Stamp ``email_verified_at`` on an Identity row directly in the DB.

    Signup auto-logs the user in before they click the verification
    email, but ``require_verified_identity`` blocks billing/admin
    access until ``email_verified_at`` is set. Tests that don't care
    about the verification flow skip that click by calling this helper.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s, s.begin():
        await s.execute(
            text("UPDATE platform_identities SET email_verified_at = now() WHERE email = :email"),
            {"email": email},
        )


# ---------------------------------------------------------------- plans seed


async def test_seed_plans_exist(owner_engine) -> None:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        codes = [
            p.code
            for p in (await session.execute(select(Plan).order_by(Plan.monthly_price_cents)))
            .scalars()
            .all()
        ]
    assert "community" in codes
    assert "starter" in codes
    assert "pro" in codes
    assert "enterprise" in codes


# ---------------------------------------------------------------- signup → trial


async def test_signup_starts_a_trial_subscription(billing_client, owner_engine) -> None:
    client, _ = billing_client
    resp = await client.post(
        "/platform/signup",
        data={
            "company_name": "TrialCo",
            "slug": "trialco",
            "owner_email": "owner@trialco.cz",
            "owner_full_name": "T",
            "password": "correct-horse-battery-staple",
            "terms_accepted": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.slug == "trialco"))
        ).scalar_one()
        subscription = (
            await session.execute(select(Subscription).where(Subscription.tenant_id == tenant.id))
        ).scalar_one()
        assert subscription.status == "trialing"
        assert subscription.trial_ends_at is not None

        # Default trial plan is Starter.
        plan = (
            await session.execute(select(Plan).where(Plan.id == subscription.plan_id))
        ).scalar_one()
        assert plan.code == "starter"


# ---------------------------------------------------------------- dashboard


async def test_billing_dashboard_renders(billing_client, owner_engine) -> None:
    client, _ = billing_client
    resp = await client.post(
        "/platform/signup",
        data={
            "company_name": "DashCo",
            "slug": "dashco",
            "owner_email": "o@dashco.cz",
            "owner_full_name": "O",
            "password": "correct-horse-battery-staple",
            "terms_accepted": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    await _mark_verified(owner_engine, "o@dashco.cz")

    resp = await client.get("/platform/billing")
    assert resp.status_code == 200
    assert "Předplatné a fakturace" in resp.text
    assert "Demo režim" in resp.text  # Stripe not configured
    # All plan cards should be present.
    for plan_name in ("Community", "Starter", "Pro", "Enterprise"):
        assert plan_name in resp.text
    # New in PR #6: usage section with the four metric labels.
    assert "Vaše spotřeba" in resp.text
    assert "Staff uživatelé" in resp.text
    assert "Kontakty klientů" in resp.text
    assert "Úložiště (MB)" in resp.text


async def test_billing_dashboard_upgrade_vs_downgrade_labels(billing_client, owner_engine) -> None:
    """The plan-chooser must visually distinguish upgrades from downgrades
    (UX-P1-#6). On the trial/Starter plan, Pro is 'Přejít nahoru',
    Community is 'Přejít dolů'."""
    client, _ = billing_client
    resp = await client.post(
        "/platform/signup",
        data={
            "company_name": "UpDnCo",
            "slug": "updn",
            "owner_email": "o@updn.cz",
            "owner_full_name": "O",
            "password": "correct-horse-battery-staple",
            "terms_accepted": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    await _mark_verified(owner_engine, "o@updn.cz")

    resp = await client.get("/platform/billing")
    assert resp.status_code == 200
    # Starter is the current plan (from the trial attached at signup).
    # Pro is more expensive → "nahoru"; Community is free → "dolů".
    assert "Přejít nahoru na Pro" in resp.text
    assert "Přejít dolů na Community" in resp.text


async def test_checkout_demo_switches_plan_locally(billing_client, owner_engine) -> None:
    client, _ = billing_client
    # Signup + initial state.
    resp = await client.post(
        "/platform/signup",
        data={
            "company_name": "UpCo",
            "slug": "upco",
            "owner_email": "o@upco.cz",
            "owner_full_name": "O",
            "password": "correct-horse-battery-staple",
            "terms_accepted": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    await _mark_verified(owner_engine, "o@upco.cz")

    # "Upgrade" to Pro in demo mode.
    resp = await client.post(
        "/platform/billing/checkout/pro",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/platform/billing?checkout=success" in resp.headers["location"]

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        tenant = (await session.execute(select(Tenant).where(Tenant.slug == "upco"))).scalar_one()
        subscription = (
            await session.execute(select(Subscription).where(Subscription.tenant_id == tenant.id))
        ).scalar_one()
        plan = (
            await session.execute(select(Plan).where(Plan.id == subscription.plan_id))
        ).scalar_one()
        assert plan.code == "pro"
        assert subscription.status == "demo"


async def test_webhook_returns_503_in_demo_mode(billing_client) -> None:
    client, _ = billing_client
    resp = await client.post("/platform/webhooks/stripe", content=b"{}")
    assert resp.status_code == 503


async def test_billing_dashboard_requires_login(billing_client) -> None:
    client, _ = billing_client
    # No signup, no session.
    resp = await client.get("/platform/billing", follow_redirects=False)
    # require_identity raises 401 → error handler bounces HTML to /auth/login.
    assert resp.status_code in (303, 401)


async def test_billing_portal_demo_mode_bounces_back(billing_client, owner_engine) -> None:
    """POST /platform/billing/portal in demo mode (or without a
    stripe_customer_id) must redirect to the billing dashboard rather
    than try to talk to Stripe."""
    client, _ = billing_client
    # Signup → trial subscription, no Stripe customer yet.
    resp = await client.post(
        "/platform/signup",
        data={
            "company_name": "PortalCo",
            "slug": "portalco",
            "owner_email": "o@portalco.cz",
            "owner_full_name": "O",
            "password": "correct-horse-battery-staple",
            "terms_accepted": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    await _mark_verified(owner_engine, "o@portalco.cz")

    resp = await client.post("/platform/billing/portal", follow_redirects=False)
    assert resp.status_code == 303
    # Demo mode + no stripe_customer_id → return to /platform/billing.
    assert resp.headers["location"].endswith("/platform/billing")


async def test_billing_dashboard_refuses_unverified(billing_client) -> None:
    """Billing must be gated behind email verification (PR #5).

    We sign up, do NOT call ``_mark_verified``, and confirm the
    dashboard responds with 403.
    """
    client, _ = billing_client
    resp = await client.post(
        "/platform/signup",
        data={
            "company_name": "UnverifiedCo",
            "slug": "unverifiedco",
            "owner_email": "o@unv.cz",
            "owner_full_name": "O",
            "password": "correct-horse-battery-staple",
            "terms_accepted": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    resp = await client.get("/platform/billing", follow_redirects=False)
    assert resp.status_code == 403


def test_checkout_honours_existing_trial_window() -> None:
    """If tenant already has ``trial_ends_at`` in the future, Stripe
    checkout must be created with ``trial_end=<timestamp>`` rather than
    a fresh ``trial_period_days=14`` — otherwise the user would get a
    second 14-day trial after their first ran out."""
    from datetime import UTC, datetime, timedelta
    from unittest.mock import MagicMock, patch

    from app.config import Settings
    from app.platform.billing.service import create_checkout_session

    settings = Settings(
        STRIPE_SECRET_KEY="sk_test_fake",
        STRIPE_WEBHOOK_SECRET="whsec_test",
    )

    tenant = MagicMock(id="11111111-1111-1111-1111-111111111111")
    tenant.stripe_customer_id = None
    plan = MagicMock(
        id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        code="pro",
        stripe_price_id="price_pro_live",
    )

    # Trial ends in 5 days — Stripe should get an absolute trial_end.
    trial_end = datetime.now(UTC) + timedelta(days=5)

    captured: dict = {}

    def _fake_create(**kwargs):
        captured.update(kwargs)
        result = MagicMock()
        result.url = "https://checkout.stripe.example/x"
        return result

    with patch("stripe.checkout.Session.create", side_effect=_fake_create):
        url = create_checkout_session(
            settings,
            tenant=tenant,
            plan=plan,
            success_url="http://x/ok",
            cancel_url="http://x/cancel",
            customer_email="o@example.com",
            trial_ends_at=trial_end,
        )

    assert url == "https://checkout.stripe.example/x"
    sub_data = captured["subscription_data"]
    # Absolute trial end is preferred over the relative day count.
    assert "trial_end" in sub_data
    assert "trial_period_days" not in sub_data
    # idempotency_key supplied to prevent double-charge on form replay.
    assert "idempotency_key" in captured
    assert captured["idempotency_key"].startswith("checkout:")
    # metadata is propagated in all three places.
    assert captured["metadata"]["tenant_id"] == str(tenant.id)
    assert sub_data["metadata"]["tenant_id"] == str(tenant.id)
    assert captured["client_reference_id"] == str(tenant.id)
    # Promo codes allowed.
    assert captured["allow_promotion_codes"] is True


def test_checkout_skips_trial_when_already_expired() -> None:
    """If the local trial already ran out, we must NOT tell Stripe to
    start a new trial."""
    from datetime import UTC, datetime, timedelta
    from unittest.mock import MagicMock, patch

    from app.config import Settings
    from app.platform.billing.service import create_checkout_session

    settings = Settings(
        STRIPE_SECRET_KEY="sk_test_fake",
        STRIPE_WEBHOOK_SECRET="whsec_test",
    )
    tenant = MagicMock(id="22222222-2222-2222-2222-222222222222")
    tenant.stripe_customer_id = None
    plan = MagicMock(id="p", code="pro", stripe_price_id="price_pro_live")
    past_trial_end = datetime.now(UTC) - timedelta(days=1)

    captured: dict = {}

    def _fake_create(**kwargs):
        captured.update(kwargs)
        result = MagicMock()
        result.url = "https://x"
        return result

    with patch("stripe.checkout.Session.create", side_effect=_fake_create):
        create_checkout_session(
            settings,
            tenant=tenant,
            plan=plan,
            success_url="http://x/ok",
            cancel_url="http://x/cancel",
            customer_email="o@example.com",
            trial_ends_at=past_trial_end,
        )

    sub_data = captured["subscription_data"]
    assert "trial_end" not in sub_data
    assert "trial_period_days" not in sub_data


def test_checkout_enables_stripe_tax_and_cs_locale() -> None:
    """CZ compliance bundle: automatic_tax + tax_id_collection +
    billing_address_collection=required + locale=cs on every checkout."""
    from unittest.mock import MagicMock, patch

    from app.config import Settings
    from app.platform.billing.service import create_checkout_session

    settings = Settings(STRIPE_SECRET_KEY="sk_test_fake")
    tenant = MagicMock(id="44444444-4444-4444-4444-444444444444")
    tenant.stripe_customer_id = None
    plan = MagicMock(id="p", code="pro", stripe_price_id="price_pro_live")

    captured: dict = {}

    def _fake_create(**kwargs):
        captured.update(kwargs)
        result = MagicMock()
        result.url = "https://x"
        return result

    with patch("stripe.checkout.Session.create", side_effect=_fake_create):
        create_checkout_session(
            settings,
            tenant=tenant,
            plan=plan,
            success_url="http://x/ok",
            cancel_url="http://x/cancel",
            customer_email="o@example.com",
        )

    # CZ market must have automatic tax + DIČ collection + address.
    assert captured["automatic_tax"] == {"enabled": True}
    assert captured["tax_id_collection"] == {"enabled": True}
    assert captured["billing_address_collection"] == "required"
    assert captured["locale"] == "cs"
    # customer_update is only for existing-customer flows; first-time
    # checkout uses customer_email, so no customer_update key.
    assert "customer_update" not in captured


def test_checkout_includes_customer_update_for_existing_customer() -> None:
    """When re-using a Stripe Customer we must pass ``customer_update``
    (alongside ``automatic_tax``) or Stripe refuses the session."""
    from unittest.mock import MagicMock, patch

    from app.config import Settings
    from app.platform.billing.service import create_checkout_session

    settings = Settings(STRIPE_SECRET_KEY="sk_test_fake")
    tenant = MagicMock(id="55555555-5555-5555-5555-555555555555")
    tenant.stripe_customer_id = "cus_existing_1"
    plan = MagicMock(id="p", code="pro", stripe_price_id="price_pro_live")

    captured: dict = {}
    with patch(
        "stripe.checkout.Session.create",
        side_effect=lambda **k: captured.update(k) or MagicMock(url="https://x"),
    ):
        create_checkout_session(
            settings,
            tenant=tenant,
            plan=plan,
            success_url="http://x/ok",
            cancel_url="http://x/cancel",
            customer_email="o@example.com",
        )

    assert captured["customer"] == "cus_existing_1"
    assert captured["customer_update"] == {"address": "auto", "name": "auto"}


def test_checkout_reuses_existing_stripe_customer() -> None:
    """When tenant.stripe_customer_id is set we pass it as ``customer=``
    and omit ``customer_email`` — no duplicate customers."""
    from unittest.mock import MagicMock, patch

    from app.config import Settings
    from app.platform.billing.service import create_checkout_session

    settings = Settings(STRIPE_SECRET_KEY="sk_test_fake")
    tenant = MagicMock(id="33333333-3333-3333-3333-333333333333")
    tenant.stripe_customer_id = "cus_existing_abc"
    plan = MagicMock(id="p", code="pro", stripe_price_id="price_pro_live")

    captured: dict = {}

    def _fake_create(**kwargs):
        captured.update(kwargs)
        result = MagicMock()
        result.url = "https://x"
        return result

    with patch("stripe.checkout.Session.create", side_effect=_fake_create):
        create_checkout_session(
            settings,
            tenant=tenant,
            plan=plan,
            success_url="http://x/ok",
            cancel_url="http://x/cancel",
            customer_email="o@example.com",
        )

    assert captured["customer"] == "cus_existing_abc"
    assert "customer_email" not in captured
