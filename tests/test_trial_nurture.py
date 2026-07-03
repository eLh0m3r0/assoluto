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


async def _seed_trial(owner_engine, tenant_id, *, status: str = "trialing") -> None:
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
            )
        )
        session.add(
            User(
                id=uuid4(),
                tenant_id=tenant_id,
                email="owner@4mex.cz",
                full_name="4MEX Owner",
                role=UserRole.TENANT_ADMIN,
                password_hash=hash_password("ownerpass"),
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


async def test_disabled_flag_sends_nothing(settings, wipe_db, owner_engine, demo_tenant) -> None:
    settings.feature_platform = True
    settings.trial_nurture_enabled = False
    await _seed_trial(owner_engine, demo_tenant.id)

    capture = CaptureSender()
    sent = await send_trial_nurture_emails(now=T0 + timedelta(days=2), sender=capture)
    assert sent == 0
    assert capture.outbox == []


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
