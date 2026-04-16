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

    resp = await client.get("/platform/billing")
    assert resp.status_code == 200
    assert "Předplatné a fakturace" in resp.text
    assert "Demo režim" in resp.text  # Stripe not configured
    # All plan cards should be present.
    for plan_name in ("Community", "Starter", "Pro", "Enterprise"):
        assert plan_name in resp.text


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
