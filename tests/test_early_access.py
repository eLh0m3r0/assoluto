"""Early access (CEO decision E1): free hosted access until EARLY_ACCESS_UNTIL.

One rule — effective trial end = max(trial_ends_at, early-access end) for
local trials — applied everywhere a trial end matters: the helper and its
SQL twin, the expiry job and the entitlement cut-off, the 14- / 3-day
reminders, signup, the billing page, the in-app banner, platform admin,
the marketing pages and the Terms.

The suite runs with early access OFF (``tests/conftest.py``); every test
here switches it on explicitly, with dates that do not depend on today
unless the test is about "today".
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.email.sender import CaptureSender, render_email
from app.main import create_app
from app.models.enums import UserRole
from app.models.tenant import Tenant
from app.models.user import User
from app.platform.billing.early_access import covered_by_early_access, effective_trial_end
from app.platform.billing.models import Plan, Subscription
from app.security.passwords import hash_password
from app.services.early_access import (
    COVERED_BY_EARLY_ACCESS_SQL,
    EARLY_ACCESS_TZ,
    EFFECTIVE_TRIAL_END_SQL,
    covered_by_early_access_for,
    early_access_ends_at,
    early_access_info,
    effective_trial_end_for,
    format_until,
    is_early_access_active,
)
from app.tasks.periodic import (
    NURTURE_SENT_KEY,
    _due_nurture_stage,
    enforce_canceled_subscriptions,
    expire_demo_trials,
    send_trial_nurture_emails,
)
from tests.conftest import CsrfAwareClient

EA_UNTIL = date(2027, 1, 31)
EA_END = early_access_ends_at(EA_UNTIL)
assert EA_END is not None
EN = {"Accept-Language": "en"}


def _today_prague() -> date:
    return datetime.now(EARLY_ACCESS_TZ).date()


# ====================================================================== rule


def test_setting_default_is_31_january_2027_and_empty_means_off() -> None:
    assert Settings.model_fields["early_access_until"].default == date(2027, 1, 31)
    assert Settings(EARLY_ACCESS_UNTIL="").early_access_until is None
    assert Settings(EARLY_ACCESS_UNTIL="2027-03-01").early_access_until == date(2027, 3, 1)


def test_end_is_the_last_second_of_the_day_in_prague() -> None:
    # Winter: CET = UTC+1.
    assert datetime(2027, 1, 31, 22, 59, 59, tzinfo=UTC) == EA_END
    # Summer: CEST = UTC+2.
    assert early_access_ends_at(date(2027, 7, 31)) == datetime(2027, 7, 31, 21, 59, 59, tzinfo=UTC)
    # Shown as the same calendar day in UTC and in Prague.
    assert EA_END.strftime("%d.%m.%Y") == "31.01.2027"
    assert EA_END.astimezone(EARLY_ACCESS_TZ).strftime("%d.%m.%Y") == "31.01.2027"
    assert early_access_ends_at(None) is None


def test_active_until_the_end_of_the_day_and_not_a_second_later() -> None:
    assert is_early_access_active(EA_UNTIL, EA_END - timedelta(days=30))
    assert is_early_access_active(EA_UNTIL, EA_END)
    assert not is_early_access_active(EA_UNTIL, EA_END + timedelta(seconds=1))
    assert not is_early_access_active(None, EA_END - timedelta(days=30))


def _eff(status="trialing", trial_ends_at=None, *, stripe=False, suspended=False, ea=EA_END):
    return effective_trial_end_for(
        status,
        trial_ends_at=trial_ends_at,
        stripe_managed=stripe,
        operator_suspended=suspended,
        early_access_end=ea,
    )


def test_effective_trial_end_boundaries() -> None:
    old_trial = datetime(2026, 10, 10, 12, tzinfo=UTC)
    late_trial = datetime(2027, 2, 20, 12, tzinfo=UTC)
    # Trial ending before the date → pushed to the date.
    assert _eff("trialing", old_trial) == EA_END
    assert _eff("demo", old_trial) == EA_END
    # Trial ending after the date → its own end.
    assert _eff("trialing", late_trial) == late_trial
    # Exactly on the boundary.
    assert _eff("trialing", EA_END) == EA_END
    # Feature off → stored value.
    assert _eff("trialing", old_trial, ea=None) == old_trial
    # Excluded: canceled, paid / active, past_due, suspended, Stripe-managed.
    for status in ("canceled", "active", "past_due", "unpaid"):
        assert _eff(status, old_trial) == old_trial, status
    assert _eff("trialing", old_trial, suspended=True) == old_trial
    assert _eff("trialing", old_trial, stripe=True) == old_trial
    # A trial without an end never ends — early access does not invent one.
    assert _eff("trialing", None) is None


def test_covered_only_when_early_access_sets_the_end() -> None:
    def cov(status, te, **kw):
        return covered_by_early_access_for(
            status,
            trial_ends_at=te,
            stripe_managed=kw.get("stripe", False),
            operator_suspended=kw.get("suspended", False),
            early_access_end=kw.get("ea", EA_END),
        )

    assert cov("trialing", datetime(2026, 10, 10, tzinfo=UTC))
    assert cov("trialing", EA_END)  # new signups store the end itself
    assert not cov("trialing", datetime(2027, 2, 20, tzinfo=UTC))
    assert not cov("active", datetime(2026, 10, 10, tzinfo=UTC))
    assert not cov("trialing", datetime(2026, 10, 10, tzinfo=UTC), suspended=True)
    assert not cov("trialing", datetime(2026, 10, 10, tzinfo=UTC), ea=None)


def test_platform_helper_reads_the_subscription_row() -> None:
    settings = Settings(EARLY_ACCESS_UNTIL="2027-01-31")
    old_trial = datetime(2026, 10, 10, 12, tzinfo=UTC)
    sub = Subscription(status="trialing", trial_ends_at=old_trial)
    assert effective_trial_end(sub, settings) == EA_END
    assert covered_by_early_access(sub, settings)

    sub.operator_suspended_at = datetime(2026, 10, 1, tzinfo=UTC)
    assert effective_trial_end(sub, settings) == old_trial
    assert not covered_by_early_access(sub, settings)

    canceled = Subscription(status="canceled", trial_ends_at=old_trial)
    assert effective_trial_end(canceled, settings) == old_trial

    stripe_trial = Subscription(
        status="trialing", trial_ends_at=old_trial, stripe_subscription_id="sub_1"
    )
    assert effective_trial_end(stripe_trial, settings) == old_trial

    off = Settings(EARLY_ACCESS_UNTIL="")
    assert effective_trial_end(Subscription(status="trialing", trial_ends_at=old_trial), off) == (
        old_trial
    )
    assert effective_trial_end(None, settings) is None


def test_long_date_follows_the_locale() -> None:
    assert format_until(EA_UNTIL, "en") == "31 January 2027"
    assert format_until(EA_UNTIL, "cs") == "31. ledna 2027"
    assert format_until(EA_UNTIL, "de") == "31. Januar 2027"
    info = early_access_info(EA_UNTIL, "en", now=EA_END + timedelta(seconds=1))
    assert not info.active and info.until_label == "31 January 2027"
    assert not early_access_info(None).active


@pytest.mark.postgres
async def test_sql_twin_agrees_with_the_python_rule(owner_engine) -> None:
    """The jobs decide in SQL, the pages in Python — they must agree."""
    old = datetime(2026, 10, 10, 12, tzinfo=UTC)
    late = datetime(2027, 2, 20, 12, tzinfo=UTC)
    suspended_at = datetime(2026, 10, 1, tzinfo=UTC)
    cases = [
        ("trialing", old, None, None),
        ("demo", old, None, None),
        ("trialing", late, None, None),
        ("trialing", EA_END, None, None),
        ("trialing", None, None, None),
        ("trialing", old, "sub_1", None),
        ("trialing", old, None, suspended_at),
        ("canceled", old, None, None),
        ("active", old, None, None),
        ("past_due", old, None, None),
    ]
    async with owner_engine.connect() as conn:
        for ea in (EA_END, None):
            for status, te, stripe_id, susp in cases:
                row = (
                    await conn.execute(
                        text(
                            f"SELECT {EFFECTIVE_TRIAL_END_SQL} AS eff, "
                            f"       {COVERED_BY_EARLY_ACCESS_SQL} AS cov "
                            "FROM (SELECT CAST(:status AS text) AS status, "
                            "             CAST(:te AS timestamptz) AS trial_ends_at, "
                            "             CAST(:sid AS text) AS stripe_subscription_id, "
                            "             CAST(:susp AS timestamptz) AS operator_suspended_at"
                            ") AS s"
                        ),
                        {
                            "status": status,
                            "te": te,
                            "sid": stripe_id,
                            "susp": susp,
                            "early_access_end": ea,
                        },
                    )
                ).one()
                kwargs = {
                    "trial_ends_at": te,
                    "stripe_managed": stripe_id is not None,
                    "operator_suspended": susp is not None,
                    "early_access_end": ea,
                }
                case = (status, te, stripe_id, susp, ea)
                assert row.eff == effective_trial_end_for(status, **kwargs), case
                assert bool(row.cov) == covered_by_early_access_for(status, **kwargs), case


# ======================================================= expiry + cut-off


async def _seed_subscription(
    owner_engine, tenant_id, *, plan_code="starter", status="trialing", **fields
) -> None:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s, s.begin():
        await s.execute(delete(Subscription).where(Subscription.tenant_id == tenant_id))
        plan = (await s.execute(select(Plan).where(Plan.code == plan_code))).scalar_one()
        sub = Subscription(tenant_id=tenant_id, plan_id=plan.id, status=status)
        for key, value in fields.items():
            setattr(sub, key, value)
        s.add(sub)


async def _sub_and_tenant(owner_engine, tenant_id) -> tuple[Subscription, Tenant]:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s:
        sub = (
            await s.execute(select(Subscription).where(Subscription.tenant_id == tenant_id))
        ).scalar_one()
        tenant = (await s.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()
        return sub, tenant


@pytest.mark.postgres
async def test_expiry_job_does_not_cut_an_early_access_trial(
    settings, owner_engine, demo_tenant
) -> None:
    settings.early_access_until = EA_UNTIL
    old_end = datetime(2026, 10, 10, 12, tzinfo=UTC)
    await _seed_subscription(
        owner_engine, demo_tenant.id, trial_ends_at=old_end, current_period_end=old_end
    )

    # Long after the stored 30-day end, still before the early-access end.
    for now in (old_end + timedelta(days=1), EA_END - timedelta(minutes=1), EA_END):
        assert await expire_demo_trials(now=now) == 0
        assert await enforce_canceled_subscriptions(now=now) == 0
    sub, tenant = await _sub_and_tenant(owner_engine, demo_tenant.id)
    assert sub.status == "trialing" and tenant.is_active

    # After the early-access end the trial expires, and the 3-day export
    # window runs from the early-access end — not from October.
    assert await expire_demo_trials(now=EA_END + timedelta(hours=1)) == 1
    sub, _ = await _sub_and_tenant(owner_engine, demo_tenant.id)
    assert sub.status == "canceled"
    assert sub.current_period_end == EA_END
    assert sub.trial_ends_at == old_end  # stored data is not rewritten
    assert await enforce_canceled_subscriptions(now=EA_END + timedelta(days=2)) == 0
    assert await enforce_canceled_subscriptions(now=EA_END + timedelta(days=3, hours=1)) == 1
    _, tenant = await _sub_and_tenant(owner_engine, demo_tenant.id)
    assert not tenant.is_active


@pytest.mark.postgres
async def test_expiry_job_without_early_access_keeps_the_old_rule(
    settings, owner_engine, demo_tenant
) -> None:
    settings.early_access_until = None
    old_end = datetime(2026, 10, 10, 12, tzinfo=UTC)
    await _seed_subscription(
        owner_engine, demo_tenant.id, trial_ends_at=old_end, current_period_end=old_end
    )
    assert await expire_demo_trials(now=old_end + timedelta(hours=1)) == 1
    sub, _ = await _sub_and_tenant(owner_engine, demo_tenant.id)
    assert sub.status == "canceled" and sub.current_period_end == old_end


@pytest.mark.postgres
async def test_expiry_job_still_cuts_a_suspended_trial(settings, owner_engine, demo_tenant) -> None:
    """Operator suspension is not covered by early access."""
    settings.early_access_until = EA_UNTIL
    old_end = datetime(2026, 10, 10, 12, tzinfo=UTC)
    await _seed_subscription(
        owner_engine,
        demo_tenant.id,
        trial_ends_at=old_end,
        current_period_end=old_end,
        operator_suspended_at=old_end - timedelta(days=1),
    )
    assert await expire_demo_trials(now=old_end + timedelta(hours=1)) == 1


@pytest.mark.postgres
async def test_expiry_job_still_ends_a_manual_paid_period(
    settings, owner_engine, demo_tenant
) -> None:
    """Paid (manually invoiced) rows keep their own end date."""
    settings.early_access_until = EA_UNTIL
    paid_until = datetime(2026, 11, 1, tzinfo=UTC)
    await _seed_subscription(
        owner_engine,
        demo_tenant.id,
        status="active",
        current_period_end=paid_until,
        status_changed_at=paid_until - timedelta(days=30),
    )
    assert await expire_demo_trials(now=paid_until + timedelta(hours=1)) == 1


# ============================================================== reminders


async def _seed_trial_admin(owner_engine, tenant_id, *, trial_ends_at, verified=True) -> None:
    from app.platform.models import Identity, TenantMembership

    await _seed_subscription(owner_engine, tenant_id, trial_ends_at=trial_ends_at)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        await session.execute(
            text("UPDATE platform_subscriptions SET created_at = :c WHERE tenant_id = :t"),
            {"c": datetime(2026, 9, 1, tzinfo=UTC), "t": tenant_id},
        )
        user = User(
            id=uuid4(),
            tenant_id=tenant_id,
            email="owner@4mex.cz",
            full_name="4MEX Owner",
            role=UserRole.TENANT_ADMIN,
            password_hash=hash_password("ownerpass"),
        )
        identity = Identity(
            id=uuid4(),
            email="owner@4mex.cz",
            full_name="4MEX Owner",
            password_hash=hash_password("ownerpass"),
            email_verified_at=datetime(2026, 9, 1, tzinfo=UTC) if verified else None,
        )
        session.add_all([user, identity])
        await session.flush()
        session.add(
            TenantMembership(
                id=uuid4(), identity_id=identity.id, tenant_id=tenant_id, user_id=user.id
            )
        )


@pytest.mark.postgres
async def test_ending_reminders_14_and_3_days_before_early_access_end_once_each(
    settings, owner_engine, demo_tenant
) -> None:
    settings.feature_platform = True
    settings.early_access_until = EA_UNTIL
    # A 30-day trial from September whose stored end passed long ago — and
    # whose old 5-day "ending" reminder was already sent for that date.
    old_end = datetime(2026, 10, 1, 12, tzinfo=UTC)
    await _seed_trial_admin(owner_engine, demo_tenant.id, trial_ends_at=old_end)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s, s.begin():
        tenant = (await s.execute(select(Tenant).where(Tenant.id == demo_tenant.id))).scalar_one()
        tenant.settings = {NURTURE_SENT_KEY: {"ending": "2026-09-26T03:00:00+00:00"}}

    capture = CaptureSender()
    morning = timedelta(hours=7)  # 08:00 Prague
    day = lambda d: datetime(2027, 1, d, tzinfo=UTC) + morning  # noqa: E731

    # Pin the recipient's language so the copy assertions below are exact.
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET preferred_locale = 'en' WHERE email = 'owner@4mex.cz'")
        )

    # 15 days before: nothing yet.
    assert await send_trial_nurture_emails(now=day(16), sender=capture) == 0
    # Exactly 14 days before (17 January): the first reminder.
    assert await send_trial_nurture_emails(now=day(17), sender=capture) == 1
    first = capture.outbox[-1]
    from app.i18n import gettext

    assert first.subject in {
        gettext(loc, "Free early access to Assoluto ends on %(trial_end_date)s")
        % {"trial_end_date": "31.01.2027"}
        for loc in ("en", "cs", "de")
    }
    # Locale-agnostic: the mail renders in the recipient's language.
    assert "4MEX s.r.o." in first.text and "31.01.2027" in first.text
    plain = first.text.replace("\xa0", " ")
    assert "1 490" in plain and "2 990" in plain
    assert "490 CZK per month for Starter" in first.text  # founding price
    assert "reply to this email" in first.text
    # …exactly once.
    for d in (18, 20, 27):
        assert await send_trial_nurture_emails(now=day(d), sender=capture) == 0
    # Exactly 3 days before (28 January): the second reminder, once.
    assert await send_trial_nurture_emails(now=day(28), sender=capture) == 1
    second = capture.outbox[-1]
    assert "ends on 31.01.2027" in second.text and "Free early access is ending" in second.text
    for d in (29, 30, 31):
        assert await send_trial_nurture_emails(now=day(d), sender=capture) == 0
    assert len(capture.outbox) == 2
    assert all(m.to == "owner@4mex.cz" for m in capture.outbox)

    _, tenant = await _sub_and_tenant(owner_engine, demo_tenant.id)
    markers = tenant.settings[NURTURE_SENT_KEY]
    assert set(markers["_ending_for"]) == {"ending14", "ending"}


@pytest.mark.postgres
async def test_ending_reminders_need_a_verified_identity(
    settings, owner_engine, demo_tenant
) -> None:
    settings.feature_platform = True
    settings.early_access_until = EA_UNTIL
    await _seed_trial_admin(
        owner_engine,
        demo_tenant.id,
        trial_ends_at=datetime(2026, 10, 1, tzinfo=UTC),
        verified=False,
    )
    capture = CaptureSender()
    for d in (17, 28):
        now = datetime(2027, 1, d, 7, tzinfo=UTC)
        assert await send_trial_nurture_emails(now=now, sender=capture) == 0
    assert capture.outbox == []


def test_reminder_stages_for_a_plain_30_day_trial() -> None:
    """Without early access the same two reminders frame a normal trial."""
    created = datetime(2026, 6, 1, 12, tzinfo=UTC)
    end = created + timedelta(days=30)  # 1 July
    assert _due_nurture_stage(end - timedelta(days=15), created, end, {}) is None
    assert _due_nurture_stage(end - timedelta(days=14), created, end, {}) == ("ending14", 14)
    assert _due_nurture_stage(end - timedelta(days=4), created, end, {}) == ("ending14", 4)
    assert _due_nurture_stage(end - timedelta(days=3), created, end, {}) == ("ending", 3)
    sent = {
        "ending14": (end - timedelta(days=14)).isoformat(),
        "_ending_for": {"ending14": end.isoformat()},
    }
    assert _due_nurture_stage(end - timedelta(days=10), created, end, sent) is None
    assert _due_nurture_stage(end - timedelta(hours=1), created, end, sent) == ("ending", 0)
    assert _due_nurture_stage(end + timedelta(hours=1), created, end, sent) is None
    # A pre-E1 "ending" mail (5-day lead) for this same end is not
    # followed by a 4-days-left "ending14".
    legacy = {"ending": (end - timedelta(days=5)).isoformat()}
    assert _due_nurture_stage(end - timedelta(days=4), created, end, legacy) is None
    # Stripe-linked trials convert by themselves — no reminders.
    assert _due_nurture_stage(end - timedelta(days=3), created, end, {}, ending_stage=False) is None


def test_ending_templates_render_both_variants_in_every_locale() -> None:
    ctx = {
        "full_name": "Jan",
        "tenant_name": "ACME",
        "billing_url": "https://assoluto.test/platform/billing",
        "trial_end_date": "31.01.2027",
        "days_left": 14,
    }
    for template in ("trial_ending14", "trial_ending"):
        for early in (True, False):
            for locale in (None, "cs", "en", "de"):
                rendered = render_email(template, {**ctx, "early_access": early}, locale=locale)
                assert "31.01.2027" in rendered.text
                assert rendered.subject.strip()
                assert "https://assoluto.test/platform/billing" in rendered.html
        en = render_email(template, {**ctx, "early_access": True}, locale="en")
        assert "Choose a plan" not in en.text  # no payment button in early access
        assert "30-day" not in en.text
        plain = render_email(template, {**ctx, "early_access": False}, locale="en")
        assert "Choose a plan" in plain.text


# ================================================================= signup


@pytest.mark.postgres
async def test_signup_during_early_access_stores_the_early_access_end(
    settings, owner_engine, demo_tenant
) -> None:
    from app.platform.billing.service import start_trial_subscription, trial_end_for_new_signup

    far = _today_prague() + timedelta(days=90)
    settings.early_access_until = far
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s, s.begin():
        tenant = (await s.execute(select(Tenant).where(Tenant.id == demo_tenant.id))).scalar_one()
        sub = await start_trial_subscription(s, tenant=tenant, plan_code="pro", settings=settings)
    assert sub.trial_ends_at == early_access_ends_at(far)
    assert sub.current_period_end == sub.trial_ends_at
    async with sm() as s:
        plan = (await s.execute(select(Plan).where(Plan.id == sub.plan_id))).scalar_one()
    assert plan.code == "pro"  # plan as chosen

    # Close to the end (or after it) the regular 30 days win.
    now = datetime(2027, 1, 20, tzinfo=UTC)
    settings.early_access_until = EA_UNTIL
    assert trial_end_for_new_signup(settings, now) == now + timedelta(days=30)
    settings.early_access_until = None
    now = datetime(2026, 10, 6, tzinfo=UTC)
    assert trial_end_for_new_signup(settings, now) == now + timedelta(days=30)


# ======================================================= billing + banner


@pytest.fixture
async def platform_app(settings, wipe_db, owner_engine) -> AsyncIterator[CsrfAwareClient]:
    settings.feature_platform = True
    settings.stripe_secret_key = ""
    async with owner_engine.begin() as conn:
        await conn.execute(text("DELETE FROM platform_tenant_memberships"))
        await conn.execute(text("DELETE FROM platform_identities"))
        await conn.execute(text("DELETE FROM platform_subscriptions"))
    from app.platform.deps import reset_platform_engine

    reset_platform_engine()
    app = create_app(settings)
    app.state.email_sender = CaptureSender()
    async with CsrfAwareClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac
    reset_platform_engine()


async def _signup(client, owner_engine, slug: str) -> None:
    resp = await client.post(
        "/platform/signup",
        data={
            "company_name": f"{slug} s.r.o.",
            "slug": slug,
            "owner_email": f"o@{slug}.cz",
            "owner_full_name": "Owner",
            "password": "correct-horse-battery-staple",
            "terms_accepted": "1",
            "plan": "pro",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE platform_identities SET email_verified_at = now() WHERE email = :e"),
            {"e": f"o@{slug}.cz"},
        )


@pytest.mark.postgres
async def test_billing_page_during_early_access_has_no_payment_pressure(
    settings, platform_app, owner_engine
) -> None:
    until = _today_prague() + timedelta(days=60)
    settings.early_access_until = until
    settings.platform_operator_email = "team@assoluto.eu"
    await _signup(platform_app, owner_engine, "eaco")

    html = (await platform_app.get("/platform/billing", headers=EN)).text
    assert "Free early access until" in html
    assert format_until(until, "en") in html
    assert 'data-early-access="billing"' in html
    assert "Want to secure the founding price?" in html
    assert "mailto:team@assoluto.eu" in html
    assert "Continue on" not in html  # no payment CTA
    assert "Pro" in html  # plan details stay

    # Feature off → the regular trial page with its "Continue on" button.
    settings.early_access_until = None
    html = (await platform_app.get("/platform/billing", headers=EN)).text
    assert "Free early access until" not in html
    assert "Continue on" in html


async def _seed_admin_login(owner_engine, tenant_id) -> None:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s, s.begin():
        s.add(
            User(
                id=uuid4(),
                tenant_id=tenant_id,
                email="admin@4mex.cz",
                full_name="Admin",
                role=UserRole.TENANT_ADMIN,
                password_hash=hash_password("adminpass"),
            )
        )


async def _admin_page(tenant_client) -> str:
    tenant_client.cookies.clear()
    resp = await tenant_client.post(
        "/auth/login",
        data={"email": "admin@4mex.cz", "password": "adminpass"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return (await tenant_client.get("/app", headers=EN)).text


@pytest.mark.postgres
async def test_in_app_banner_is_quiet_until_the_last_14_days(
    tenant_client, owner_engine, demo_tenant, settings
) -> None:
    settings.feature_platform = True
    await _seed_admin_login(owner_engine, demo_tenant.id)
    # The stored 30-day trial ends in 2 days — early access covers it.
    stored_end = datetime.now(UTC) + timedelta(days=2)
    await _seed_subscription(owner_engine, demo_tenant.id, trial_ends_at=stored_end)

    settings.early_access_until = _today_prague() + timedelta(days=40)
    page = await _admin_page(tenant_client)
    assert 'data-banner="early-access"' not in page
    assert "Choose a plan" not in page  # no countdown pressure
    assert stored_end.strftime("%d.%m.%Y") not in page

    settings.early_access_until = _today_prague() + timedelta(days=10)
    page = await _admin_page(tenant_client)
    assert 'data-banner="early-access"' in page
    assert "Free early access" in page
    assert early_access_ends_at(settings.early_access_until).strftime("%d.%m.%Y") in page
    assert "Choose a plan" not in page

    # Feature off: the regular amber countdown on the stored date.
    settings.early_access_until = None
    page = await _admin_page(tenant_client)
    assert 'data-banner="early-access"' not in page
    assert "Choose a plan" in page
    assert stored_end.strftime("%d.%m.%Y") in page


# ========================================================= platform admin


async def _login_root(platform_app, owner_engine) -> None:
    from app.platform.models import Identity

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s, s.begin():
        s.add(
            Identity(
                id=uuid4(),
                email="root@platform.local",
                full_name="Root",
                password_hash=hash_password("rootpass"),
                is_platform_admin=True,
                email_verified_at=datetime.now(UTC),
            )
        )
    platform_app.cookies.clear()
    resp = await platform_app.post(
        "/platform/login",
        data={"email": "root@platform.local", "password": "rootpass"},
        follow_redirects=False,
    )
    assert resp.status_code == 303


@pytest.mark.postgres
async def test_platform_admin_dashboard_shows_date_and_covered_tenants(
    settings, platform_app, owner_engine
) -> None:
    until = _today_prague() + timedelta(days=60)
    settings.early_access_until = until
    await _signup(platform_app, owner_engine, "coveredco")
    await _login_root(platform_app, owner_engine)

    html = (await platform_app.get("/platform/admin/dashboard", headers=EN)).text
    block = html.split('data-early-access="dashboard"', 1)[1].split("</div>", 1)[0]
    assert until.strftime("%d.%m.%Y") in block
    assert re.search(r"Tenants covered:\s*<strong>1</strong>", block)

    tenants = (await platform_app.get("/platform/admin/tenants", headers=EN)).text
    assert early_access_ends_at(until).strftime("%d.%m.%Y") in tenants
    assert "early access" in tenants


# ============================================================== marketing


@pytest.fixture
async def www(settings) -> AsyncIterator[CsrfAwareClient]:
    settings.platform_operator_name = "Jan Provozovatel"
    settings.platform_operator_ico = "12345678"
    settings.platform_operator_address = "Masarykova 1, 405 02 Děčín"
    app = create_app(settings)
    async with CsrfAwareClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac


def _jsonld(html: str) -> list[dict]:
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, flags=re.S)
    return [json.loads(b) for b in blocks]


async def test_pricing_shows_early_access_while_the_date_is_ahead(settings, www) -> None:
    until = _today_prague() + timedelta(days=30)
    settings.early_access_until = until
    label = format_until(until, "en")
    html = (await www.get("/pricing", headers=EN)).text
    assert 'data-early-access="banner"' in html
    assert f"Early access: free until {label}." in html
    assert "Then 1 490 CZK / month, your data stays." in html
    assert "The first 10 workshops keep the founding price of 490 CZK for good." in html
    assert "Start free" in html
    assert "Start 30-day trial" not in html
    assert "30 days free" not in html
    # Meta + JSON-LD say the same thing.
    assert 'content="Pricing: Starter 1 490 CZK / month' in html
    assert f"Early access: free until {label}, no card." in html
    offer = next(d for d in _jsonld(html) if d.get("@type") == "SoftwareApplication")["offers"]
    assert offer["description"] == f"Free early access until {label}, no credit card required"


async def test_pricing_flips_back_once_the_date_has_passed(settings, www) -> None:
    settings.early_access_until = _today_prague() - timedelta(days=1)
    html = (await www.get("/pricing", headers=EN)).text
    assert 'data-early-access="banner"' not in html
    assert "Early access" not in html
    assert "Start 30-day trial" in html
    assert "30 days free. No card. Cancel any time." in html
    offer = next(d for d in _jsonld(html) if d.get("@type") == "SoftwareApplication")["offers"]
    assert offer["description"] == "30-day free trial, no credit card required"


@pytest.mark.postgres
async def test_homepage_hero_banner_and_ctas_follow_the_date(settings, wipe_db) -> None:
    from app.platform.deps import reset_platform_engine

    settings.feature_platform = True
    settings.default_tenant_slug = None
    reset_platform_engine()
    app = create_app(settings)
    async with CsrfAwareClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as home:
        until = _today_prague() + timedelta(days=30)
        settings.early_access_until = until
        html = (await home.get("/", headers=EN)).text
        hero = html.split("<h1", 1)[1].split("_hero_mock", 1)[0]
        assert 'data-early-access="banner"' in html
        assert "Start free" in html and "Try free for 30 days" not in html
        assert f"Early access: free until {format_until(until, 'en')}" in html
        offers = next(d for d in _jsonld(html) if d.get("@type") == "SoftwareApplication")["offers"]
        assert all("30-day" not in o.get("description", "") for o in offers)
        assert hero  # banner sits in the hero section
        assert html.index('data-early-access="banner"') < html.index("The situation today")

        settings.early_access_until = _today_prague() - timedelta(days=1)
        html = (await home.get("/", headers=EN)).text
        assert 'data-early-access="banner"' not in html
        assert "Try free for 30 days" in html and "Start free" not in html
        settings.early_access_until = None
        html = (await home.get("/", headers=EN)).text
        assert 'data-early-access="banner"' not in html
    reset_platform_engine()


async def test_terms_carry_the_early_access_clause_and_version_1_2(settings, www) -> None:
    settings.early_access_until = EA_UNTIL
    html = (await www.get("/terms", headers=EN)).text
    assert "Version 1.2" in html
    assert "Changes in 1.2" in html and "Changes in 1.1" in html
    assert 'id="early-access"' in html
    assert "Until the end of 31 January 2027" in html
    assert "No payment card is required." in html
    assert "14 days and 3 days before Early Access ends" in html
    assert "Either party may end the use of the Hosted Service during Early Access" in html
    settings.early_access_until = None
    html = (await www.get("/terms", headers=EN)).text
    assert 'id="early-access"' not in html


@pytest.mark.postgres
async def test_operator_extension_counts_from_the_effective_end(
    settings, platform_app, owner_engine
) -> None:
    until = _today_prague() + timedelta(days=60)
    settings.early_access_until = until
    await _signup(platform_app, owner_engine, "extendco")
    async with owner_engine.begin() as conn:
        # A trial stored before early access existed: ends in two days.
        await conn.execute(
            text("UPDATE platform_subscriptions SET trial_ends_at = now() + interval '2 days'")
        )
        tenant_id = (
            await conn.execute(text("SELECT id FROM tenants WHERE slug = 'extendco'"))
        ).scalar_one()
    await _login_root(platform_app, owner_engine)

    edit = (
        await platform_app.get(f"/platform/admin/tenants/{tenant_id}/subscription", headers=EN)
    ).text
    assert 'data-early-access="admin"' in edit

    resp = await platform_app.post(
        f"/platform/admin/tenants/{tenant_id}/subscription",
        data={"quick_action": "extend_trial:30"},
        follow_redirects=False,
    )
    assert "notice=" in resp.headers["location"]
    sub, _ = await _sub_and_tenant(owner_engine, tenant_id)
    assert sub.trial_ends_at == early_access_ends_at(until) + timedelta(days=30)


async def test_stripe_checkout_during_early_access_starts_billing_after_it() -> None:
    """A card added during early access is not charged before the date."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.platform.billing.service import open_checkout_session

    until = _today_prague() + timedelta(days=60)
    settings = Settings(
        STRIPE_SECRET_KEY="sk_test_fake",
        STRIPE_WEBHOOK_SECRET="whsec_test",
        EARLY_ACCESS_UNTIL=until.isoformat(),
    )
    tenant = MagicMock(id="11111111-1111-1111-1111-111111111111", stripe_customer_id=None)
    plan = MagicMock(id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", code="pro")
    plan.stripe_price_id = "price_pro"
    sub = Subscription(
        id=uuid4(), status="trialing", trial_ends_at=datetime.now(UTC) + timedelta(days=3)
    )
    captured: dict = {}

    def _fake_create(**kwargs):
        captured.update(kwargs)
        return MagicMock(url="https://checkout.stripe.example/x", id="cs_1")

    with patch("stripe.checkout.Session.create", side_effect=_fake_create):
        await open_checkout_session(
            AsyncMock(),
            settings,
            tenant=tenant,
            subscription=sub,
            plan=plan,
            success_url="http://x/ok",
            cancel_url="http://x/cancel",
            customer_email="o@example.com",
        )
    ea_end = early_access_ends_at(until)
    assert ea_end is not None
    assert captured["subscription_data"]["trial_end"] == int(ea_end.timestamp())
