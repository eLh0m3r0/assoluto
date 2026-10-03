"""Tests for the trial-nurture cadence (F-BIZ-003).

Exercises ``send_trial_nurture_emails``: the feature-flag gate, the
day-1 / day-7 / trial-ending send windows, idempotence via the
``tenants.settings`` marker, and the window-passed / non-trial skips.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.email.sender import CaptureSender
from app.models.enums import UserRole
from app.models.tenant import Tenant
from app.models.user import User
from app.platform.billing.models import Plan, Subscription
from app.security.passwords import hash_password
from app.tasks.periodic import NURTURE_SENT_KEY, send_trial_nurture_emails

pytestmark = pytest.mark.postgres

T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
TRIAL_END = T0 + timedelta(days=30)


async def _seed_trial(
    owner_engine,
    tenant_id,
    *,
    status: str = "trialing",
    verified: bool = True,
    stripe_subscription_id: str | None = None,
) -> None:
    """Trial subscription + one tenant admin linked to a platform identity.

    ``verified`` stamps the identity's ``email_verified_at`` — nurture
    mail only ever goes to confirmed addresses (BIZ-09).
    """
    from app.platform.models import Identity, TenantMembership

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        plan = Plan(id=uuid4(), code=f"test-{uuid4().hex[:8]}", name="Test plan")
        session.add(plan)
        await session.flush()
        session.add(
            Subscription(
                id=uuid4(),
                tenant_id=tenant_id,
                plan_id=plan.id,
                status=status,
                trial_ends_at=TRIAL_END,
                created_at=T0,
                stripe_subscription_id=stripe_subscription_id,
            )
        )
        user = User(
            id=uuid4(),
            tenant_id=tenant_id,
            email="owner@4mex.cz",
            full_name="4MEX Owner",
            role=UserRole.TENANT_ADMIN,
            password_hash=hash_password("ownerpass"),
        )
        session.add(user)
        identity = Identity(
            id=uuid4(),
            email="owner@4mex.cz",
            full_name="4MEX Owner",
            password_hash=hash_password("ownerpass"),
            email_verified_at=T0 if verified else None,
        )
        session.add(identity)
        await session.flush()
        session.add(
            TenantMembership(
                id=uuid4(), identity_id=identity.id, tenant_id=tenant_id, user_id=user.id
            )
        )


async def _sent_markers(owner_engine, tenant_id) -> dict:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        tenant = (await session.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()
        return (tenant.settings or {}).get(NURTURE_SENT_KEY) or {}


def _enable(settings) -> None:
    settings.feature_platform = True
    settings.trial_nurture_enabled = True


async def test_disabled_flag_sends_no_onboarding_mail(
    settings, wipe_db, owner_engine, demo_tenant
) -> None:
    settings.feature_platform = True
    settings.trial_nurture_enabled = False
    await _seed_trial(owner_engine, demo_tenant.id)

    capture = CaptureSender()
    sent = await send_trial_nurture_emails(now=T0 + timedelta(days=2), sender=capture)
    assert sent == 0
    assert capture.outbox == []


async def test_trial_ending_reminder_sent_even_with_flag_off(
    settings, wipe_db, owner_engine, demo_tenant
) -> None:
    """LOGIC-4: the copy-approval flag gates the onboarding mails only.
    A trial must never end silently."""
    settings.feature_platform = True
    settings.trial_nurture_enabled = False
    await _seed_trial(owner_engine, demo_tenant.id)

    capture = CaptureSender()
    sent = await send_trial_nurture_emails(now=TRIAL_END - timedelta(days=3), sender=capture)
    assert sent == 1
    assert capture.outbox[0].to == "owner@4mex.cz"
    assert "01.07.2026" in capture.outbox[0].text


async def test_unverified_identity_gets_no_nurture(
    settings, wipe_db, owner_engine, demo_tenant
) -> None:
    """BIZ-09: bot signups never confirm the address — do not mail them."""
    _enable(settings)
    await _seed_trial(owner_engine, demo_tenant.id, verified=False)

    capture = CaptureSender()
    assert await send_trial_nurture_emails(now=T0 + timedelta(days=2), sender=capture) == 0
    assert await send_trial_nurture_emails(now=TRIAL_END - timedelta(days=3), sender=capture) == 0
    assert capture.outbox == []


async def test_support_access_user_gets_no_nurture(
    settings, wipe_db, owner_engine, demo_tenant
) -> None:
    """LOGIC-20: the operator's support grant is not the customer."""
    from app.platform.models import Identity, TenantMembership

    _enable(settings)
    await _seed_trial(owner_engine, demo_tenant.id)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        support_user = User(
            id=uuid4(),
            tenant_id=demo_tenant.id,
            email="root@platform.local",
            full_name="root",
            role=UserRole.TENANT_ADMIN,
        )
        session.add(support_user)
        ident = Identity(
            id=uuid4(),
            email="root@platform.local",
            full_name="root",
            password_hash=hash_password("x" * 12),
            is_platform_admin=True,
            email_verified_at=T0,
        )
        session.add(ident)
        await session.flush()
        session.add(
            TenantMembership(
                id=uuid4(),
                identity_id=ident.id,
                tenant_id=demo_tenant.id,
                user_id=support_user.id,
                access_type="support",
            )
        )

    capture = CaptureSender()
    await send_trial_nurture_emails(now=T0 + timedelta(days=2), sender=capture)
    assert [m.to for m in capture.outbox] == ["owner@4mex.cz"]


async def test_stripe_linked_trial_gets_no_ending_reminder(
    settings, wipe_db, owner_engine, demo_tenant
) -> None:
    """A Stripe trial with a card converts by itself — "pick a plan"
    would be wrong."""
    _enable(settings)
    await _seed_trial(owner_engine, demo_tenant.id, stripe_subscription_id="sub_1")
    capture = CaptureSender()
    assert await send_trial_nurture_emails(now=TRIAL_END - timedelta(days=3), sender=capture) == 0


async def test_day1_sent_once(settings, wipe_db, owner_engine, demo_tenant) -> None:
    _enable(settings)
    await _seed_trial(owner_engine, demo_tenant.id)

    capture = CaptureSender()
    sent = await send_trial_nurture_emails(now=T0 + timedelta(days=2), sender=capture)
    assert sent == 1
    assert len(capture.outbox) == 1
    mail = capture.outbox[0]
    assert mail.to == "owner@4mex.cz"
    assert "4MEX Owner" in mail.text
    assert demo_tenant.slug in mail.text  # portal URL carries the subdomain
    assert "day1" in (await _sent_markers(owner_engine, demo_tenant.id))

    # Second run in the same window: idempotent, nothing new.
    again = await send_trial_nurture_emails(now=T0 + timedelta(days=3), sender=capture)
    assert again == 0
    assert len(capture.outbox) == 1


async def test_day7_and_ending_windows(settings, wipe_db, owner_engine, demo_tenant) -> None:
    _enable(settings)
    await _seed_trial(owner_engine, demo_tenant.id)

    capture = CaptureSender()
    sent = await send_trial_nurture_emails(now=T0 + timedelta(days=8), sender=capture)
    assert sent == 1
    markers = await _sent_markers(owner_engine, demo_tenant.id)
    assert "day7" in markers and "day1" not in markers  # day1 window passed — skipped

    sent = await send_trial_nurture_emails(now=TRIAL_END - timedelta(days=3), sender=capture)
    assert sent == 1
    assert "ending" in (await _sent_markers(owner_engine, demo_tenant.id))
    ending_mail = capture.outbox[-1]
    assert "01.07.2026" in ending_mail.text  # trial_end_date formatting
    assert "/platform/billing" in ending_mail.text

    # After the trial has ended: nothing more, ever.
    sent = await send_trial_nurture_emails(now=TRIAL_END + timedelta(days=1), sender=capture)
    assert sent == 0


async def test_between_windows_sends_nothing(settings, wipe_db, owner_engine, demo_tenant) -> None:
    _enable(settings)
    await _seed_trial(owner_engine, demo_tenant.id)

    capture = CaptureSender()
    sent = await send_trial_nurture_emails(now=T0 + timedelta(days=5), sender=capture)
    assert sent == 0
    assert await _sent_markers(owner_engine, demo_tenant.id) == {}


async def test_non_trial_subscription_skipped(settings, wipe_db, owner_engine, demo_tenant) -> None:
    _enable(settings)
    await _seed_trial(owner_engine, demo_tenant.id, status="active")

    capture = CaptureSender()
    sent = await send_trial_nurture_emails(now=T0 + timedelta(days=2), sender=capture)
    assert sent == 0
    assert capture.outbox == []
