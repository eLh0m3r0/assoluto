"""Regression tests for the 2026-10-03 audit — billing operations.

Entitlement job (Codex-8, LOGIC-18), the platform-admin subscription
editor (LOGIC-18 / SEC-11), operator suspension (Codex-6), the MRR tile
(BIZ-09) and platform logout (SEC-6).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.main import create_app
from app.models.tenant import Tenant
from app.platform.billing.models import Plan, Subscription
from app.platform.models import Identity
from app.security.passwords import hash_password
from app.tasks.periodic import (
    PAST_DUE_GRACE_DAYS,
    access_cutoff,
    enforce_canceled_subscriptions,
    expire_demo_trials,
)
from tests.conftest import CsrfAwareClient

pytestmark = pytest.mark.postgres

NOW = datetime.now(UTC)


async def _seed(owner_engine, *, status: str, active: bool = True, **sub_fields) -> Tenant:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s, s.begin():
        tenant = Tenant(
            id=uuid4(),
            slug=f"ops-{uuid4().hex[:8]}",
            name="Ops Co",
            billing_email=f"ops-{uuid4().hex[:6]}@example.com",
            storage_prefix=f"ops-{uuid4().hex[:8]}/",
            is_active=active,
        )
        s.add(tenant)
        await s.flush()
        starter = (await s.execute(select(Plan).where(Plan.code == "starter"))).scalar_one()
        sub = Subscription(tenant_id=tenant.id, plan_id=starter.id, status=status)
        for k, v in sub_fields.items():
            setattr(sub, k, v)
        s.add(sub)
    return tenant


async def _tenant_active(owner_engine, tenant_id) -> bool:
    async with owner_engine.connect() as conn:
        return bool(
            (
                await conn.execute(
                    text("SELECT is_active FROM tenants WHERE id = :id"), {"id": tenant_id}
                )
            ).scalar_one()
        )


async def _sub(owner_engine, tenant_id) -> Subscription:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s:
        return (
            await s.execute(select(Subscription).where(Subscription.tenant_id == tenant_id))
        ).scalar_one()


# ------------------------------------------------------------ Codex-8 rules


@pytest.mark.parametrize(
    ("status", "age_days", "cut"),
    [
        ("past_due", PAST_DUE_GRACE_DAYS + 1, True),
        ("past_due", 2, False),
        ("unpaid", 4, True),
        ("unpaid", 1, False),
        ("incomplete_expired", 4, True),
        ("incomplete", 4, True),
        ("paused", 4, True),
        ("active", 400, False),
    ],
)
async def test_codex8_every_non_paying_status_has_a_cutoff(
    owner_engine, wipe_db, status, age_days, cut
) -> None:
    changed = NOW - timedelta(days=age_days)
    tenant = await _seed(
        owner_engine,
        status=status,
        stripe_subscription_id="sub_x",
        current_period_end=NOW + timedelta(days=20),
    )
    # The ORM listener stamped "now" at insert; backdate explicitly.
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE platform_subscriptions SET status_changed_at = :c WHERE tenant_id = :t"),
            {"c": changed, "t": tenant.id},
        )
    await enforce_canceled_subscriptions(now=NOW)
    assert await _tenant_active(owner_engine, tenant.id) is (not cut)


def test_access_cutoff_matches_the_rules() -> None:
    t = NOW
    assert access_cutoff("active", current_period_end=t, status_changed_at=t) is None
    assert access_cutoff("canceled", current_period_end=t, status_changed_at=None) == t + timedelta(
        days=3
    )
    assert access_cutoff("past_due", current_period_end=None, status_changed_at=t) == t + timedelta(
        days=PAST_DUE_GRACE_DAYS
    )
    assert access_cutoff("unpaid", current_period_end=None, status_changed_at=t) == t + timedelta(
        days=3
    )


async def test_manual_active_with_end_date_expires(owner_engine, wipe_db) -> None:
    """LOGIC-18: "set active" used to mean free forever."""
    tenant = await _seed(
        owner_engine,
        status="active",
        current_period_end=NOW - timedelta(days=1),
        status_changed_at=NOW - timedelta(days=40),
    )
    assert await expire_demo_trials(now=NOW) == 1
    sub = await _sub(owner_engine, tenant.id)
    assert sub.status == "canceled"
    assert sub.canceled_at is not None


async def test_legacy_manual_active_without_marker_is_left_alone(owner_engine, wipe_db) -> None:
    """Rows from before the editor change (status_changed_at NULL) — e.g.
    a real customer billed by hand — must not be cut by a date that was
    never meant as an end date."""
    tenant = await _seed(owner_engine, status="active", current_period_end=NOW - timedelta(days=60))
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE platform_subscriptions SET status_changed_at = NULL WHERE tenant_id = :t"),
            {"t": tenant.id},
        )
    assert await expire_demo_trials(now=NOW) == 0
    assert (await _sub(owner_engine, tenant.id)).status == "active"


# ---------------------------------------------------- platform admin client


@pytest.fixture
async def admin_client(settings, wipe_db, owner_engine) -> AsyncIterator[CsrfAwareClient]:
    settings.feature_platform = True
    from app.platform.deps import reset_platform_engine

    reset_platform_engine()
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s, s.begin():
        s.add(
            Identity(
                id=uuid4(),
                email="root@platform.local",
                full_name="Root",
                password_hash=hash_password("rootpass"),
                is_platform_admin=True,
                email_verified_at=NOW,
            )
        )
    app = create_app(settings)
    async with CsrfAwareClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        resp = await ac.post(
            "/platform/login",
            data={"email": "root@platform.local", "password": "rootpass"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        yield ac
    reset_platform_engine()


async def test_subscription_editor_rejects_malformed_quick_action(
    admin_client, owner_engine
) -> None:
    """SEC-11: ``extend_trial:abc`` was an unguarded int() → 500."""
    tenant = await _seed(owner_engine, status="trialing", trial_ends_at=NOW)
    for bad in ("extend_trial:abc", "extend_trial:-5", "extend_trial:99999", "drop_tables"):
        resp = await admin_client.post(
            f"/platform/admin/tenants/{tenant.id}/subscription",
            data={"quick_action": bad},
            follow_redirects=False,
        )
        assert resp.status_code == 303, bad
        assert "error=" in resp.headers["location"], bad


async def test_subscription_editor_refuses_stripe_managed(admin_client, owner_engine) -> None:
    tenant = await _seed(owner_engine, status="active", stripe_subscription_id="sub_live")
    resp = await admin_client.post(
        f"/platform/admin/tenants/{tenant.id}/subscription",
        data={"quick_action": "extend_trial:30"},
        follow_redirects=False,
    )
    assert "error=" in resp.headers["location"]
    assert (await _sub(owner_engine, tenant.id)).status == "active"


async def test_set_active_requires_an_end_date(admin_client, owner_engine) -> None:
    tenant = await _seed(owner_engine, status="trialing", trial_ends_at=NOW)
    url = f"/platform/admin/tenants/{tenant.id}/subscription"
    resp = await admin_client.post(url, data={"quick_action": "set_active"}, follow_redirects=False)
    assert "error=" in resp.headers["location"]
    assert (await _sub(owner_engine, tenant.id)).status == "trialing"

    until = (NOW + timedelta(days=365)).date().isoformat()
    resp = await admin_client.post(
        url, data={"quick_action": "set_active", "active_until": until}, follow_redirects=False
    )
    assert "notice=" in resp.headers["location"]
    sub = await _sub(owner_engine, tenant.id)
    assert sub.status == "active"
    assert sub.current_period_end.date().isoformat() == until
    assert sub.status_changed_at is not None


async def test_extend_trial_lifts_billing_cut_but_not_operator_suspension(
    admin_client, owner_engine
) -> None:
    lapsed = await _seed(
        owner_engine, status="canceled", active=False, current_period_end=NOW - timedelta(days=9)
    )
    suspended = await _seed(
        owner_engine,
        status="canceled",
        active=False,
        current_period_end=NOW - timedelta(days=9),
        operator_suspended_at=NOW,
    )
    for t in (lapsed, suspended):
        resp = await admin_client.post(
            f"/platform/admin/tenants/{t.id}/subscription",
            data={"quick_action": "extend_trial:30"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
    assert await _tenant_active(owner_engine, lapsed.id) is True
    assert (await _sub(owner_engine, lapsed.id)).status == "trialing"
    assert await _tenant_active(owner_engine, suspended.id) is False


async def test_operator_deactivation_is_recorded_and_cleared(admin_client, owner_engine) -> None:
    tenant = await _seed(owner_engine, status="trialing", trial_ends_at=NOW + timedelta(days=9))
    await admin_client.post(f"/platform/admin/tenants/{tenant.id}/deactivate")
    sub = await _sub(owner_engine, tenant.id)
    assert sub.operator_suspended_at is not None
    assert await _tenant_active(owner_engine, tenant.id) is False

    await admin_client.post(f"/platform/admin/tenants/{tenant.id}/reactivate")
    sub = await _sub(owner_engine, tenant.id)
    assert sub.operator_suspended_at is None
    assert await _tenant_active(owner_engine, tenant.id) is True


async def test_mrr_counts_only_paid_subscriptions(admin_client, owner_engine) -> None:
    """BIZ-09: trials and demo rows were summed into MRR."""
    await _seed(owner_engine, status="trialing")
    await _seed(owner_engine, status="demo")
    await _seed(owner_engine, status="active", stripe_subscription_id="sub_paid")
    resp = await admin_client.get("/platform/admin/dashboard")
    assert resp.status_code == 200
    async with owner_engine.connect() as conn:
        starter_cents, currency = (
            await conn.execute(
                text(
                    "SELECT monthly_price_cents, currency FROM platform_plans "
                    "WHERE code = 'starter'"
                )
            )
        ).one()
    from app.templating import _money_filter

    # Exactly one paid Starter: the tile must not show three times the price.
    assert _money_filter(starter_cents, currency) in resp.text
    assert _money_filter(starter_cents * 3, currency) not in resp.text


# ------------------------------------------------------------------ SEC-6


async def test_platform_logout_invalidates_copied_cookie(admin_client) -> None:
    from app.platform.session import PLATFORM_COOKIE_NAME

    stolen = admin_client.cookies.get(PLATFORM_COOKIE_NAME)
    assert stolen
    resp = await admin_client.post("/platform/logout", follow_redirects=False)
    assert resp.status_code == 303
    admin_client.cookies.set(PLATFORM_COOKIE_NAME, stolen)
    resp = await admin_client.get("/platform/admin/tenants", follow_redirects=False)
    assert resp.status_code in (303, 401)


async def test_billing_jobs_leave_an_audit_trail(owner_engine, wipe_db) -> None:
    """BE-13: jobs disabled tenants without a trace."""
    tenant = await _seed(
        owner_engine,
        status="trialing",
        trial_ends_at=NOW - timedelta(days=10),
        current_period_end=NOW - timedelta(days=10),
    )
    assert await expire_demo_trials(now=NOW) == 1
    assert await enforce_canceled_subscriptions(now=NOW) == 1
    async with owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT action, actor_type, actor_label FROM audit_events "
                    "WHERE tenant_id = :t ORDER BY occurred_at"
                ),
                {"t": tenant.id},
            )
        ).all()
    assert [r.action for r in rows] == [
        "billing.subscription_expired",
        "tenant.deactivated_for_billing",
    ]
    assert {r.actor_type for r in rows} == {"system"}
    assert rows[1].actor_label == "job:enforce_canceled_subscriptions"
