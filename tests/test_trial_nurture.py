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


# ------------------------------------------------ verification gate (BIZ-09)


async def _seed_owner_identity(owner_engine, tenant_id, *, verified: bool) -> None:
    """Link the seeded owner to a platform Identity, as signup does."""
    from app.platform.models import Identity, TenantMembership

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        owner = (
            await session.execute(select(User).where(User.email == "owner@4mex.cz"))
        ).scalar_one()
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
                identity_id=identity.id,
                tenant_id=tenant_id,
                user_id=owner.id,
                access_type="member",
            )
        )


async def test_unverified_signup_gets_no_nurture(
    settings, wipe_db, owner_engine, demo_tenant
) -> None:
    _enable(settings)
    await _seed_trial(owner_engine, demo_tenant.id)
    await _seed_owner_identity(owner_engine, demo_tenant.id, verified=False)

    capture = CaptureSender()
    sent = await send_trial_nurture_emails(now=T0 + timedelta(days=2), sender=capture)
    assert sent == 0
    assert capture.outbox == []
    assert await _sent_markers(owner_engine, demo_tenant.id) == {}


async def test_verified_signup_gets_nurture(settings, wipe_db, owner_engine, demo_tenant) -> None:
    _enable(settings)
    await _seed_trial(owner_engine, demo_tenant.id)
    await _seed_owner_identity(owner_engine, demo_tenant.id, verified=True)

    capture = CaptureSender()
    sent = await send_trial_nurture_emails(now=T0 + timedelta(days=2), sender=capture)
    assert sent == 1
    assert capture.outbox[0].to == "owner@4mex.cz"


# ------------------------------------------- activation nudges (BIZ-16)


def _enable_activation_only(settings) -> None:
    settings.feature_platform = True
    settings.trial_nurture_enabled = False
    settings.activation_nudges_enabled = True


async def _seed_contact(owner_engine, tenant_id, *, accepted: bool) -> None:
    from app.models.customer import Customer, CustomerContact

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        customer = Customer(id=uuid4(), tenant_id=tenant_id, name="Strojírna Ukázková s.r.o.")
        session.add(customer)
        await session.flush()
        session.add(
            CustomerContact(
                id=uuid4(),
                tenant_id=tenant_id,
                customer_id=customer.id,
                email="nakup@ukazkova.test",
                full_name="Petra Nákupčí",
                invited_at=T0,
                accepted_at=T0 + timedelta(days=1) if accepted else None,
                password_hash=hash_password("contactpass") if accepted else None,
            )
        )


async def test_activation_flag_off_sends_nothing(
    settings, wipe_db, owner_engine, demo_tenant
) -> None:
    settings.feature_platform = True
    settings.trial_nurture_enabled = False
    settings.activation_nudges_enabled = False
    await _seed_trial(owner_engine, demo_tenant.id)

    capture = CaptureSender()
    sent = await send_trial_nurture_emails(now=T0 + timedelta(days=3), sender=capture)
    assert sent == 0


async def test_invite_nudge_when_no_customer_invited(
    settings, wipe_db, owner_engine, demo_tenant
) -> None:
    _enable_activation_only(settings)
    await _seed_trial(owner_engine, demo_tenant.id)

    capture = CaptureSender()
    # Day 1: the trial day-1 mail is off and the invite nudge is not due yet.
    assert await send_trial_nurture_emails(now=T0 + timedelta(days=1, hours=1), sender=capture) == 0

    sent = await send_trial_nurture_emails(now=T0 + timedelta(days=2, hours=1), sender=capture)
    assert sent == 1
    mail = capture.outbox[0]
    assert f"{demo_tenant.slug}." in mail.text and "/app/customers" in mail.text
    assert "invite" in (await _sent_markers(owner_engine, demo_tenant.id))

    # Idempotent.
    assert await send_trial_nurture_emails(now=T0 + timedelta(days=3), sender=capture) == 0


async def test_invite_nudge_skipped_when_customer_already_invited(
    settings, wipe_db, owner_engine, demo_tenant
) -> None:
    _enable_activation_only(settings)
    await _seed_trial(owner_engine, demo_tenant.id)
    await _seed_contact(owner_engine, demo_tenant.id, accepted=True)

    capture = CaptureSender()
    sent = await send_trial_nurture_emails(now=T0 + timedelta(days=2, hours=1), sender=capture)
    assert sent == 0
    assert capture.outbox == []


async def test_no_login_nudge_lists_pending_contact(
    settings, wipe_db, owner_engine, demo_tenant
) -> None:
    _enable_activation_only(settings)
    await _seed_trial(owner_engine, demo_tenant.id)
    await _seed_contact(owner_engine, demo_tenant.id, accepted=False)

    capture = CaptureSender()
    # Day 2: a contact was invited, so "invite your first customer" is moot.
    assert await send_trial_nurture_emails(now=T0 + timedelta(days=2, hours=1), sender=capture) == 0

    sent = await send_trial_nurture_emails(now=T0 + timedelta(days=5, hours=1), sender=capture)
    assert sent == 1
    mail = capture.outbox[0]
    assert "Petra Nákupčí" in mail.text
    assert "Strojírna Ukázková s.r.o." in mail.text
    assert "/app/customers/" in mail.text
    assert "no_login" in (await _sent_markers(owner_engine, demo_tenant.id))


async def test_no_login_nudge_skipped_when_contact_signed_in(
    settings, wipe_db, owner_engine, demo_tenant
) -> None:
    _enable_activation_only(settings)
    await _seed_trial(owner_engine, demo_tenant.id)
    await _seed_contact(owner_engine, demo_tenant.id, accepted=True)

    capture = CaptureSender()
    sent = await send_trial_nurture_emails(now=T0 + timedelta(days=5, hours=1), sender=capture)
    assert sent == 0


def test_activation_templates_render_in_every_locale() -> None:
    from app.email.sender import render_email

    ctx = {
        "full_name": "Jan",
        "tenant_name": "ACME",
        "portal_url": "https://acme.example.test",
        "customers_url": "https://acme.example.test/app/customers",
        "pending_contacts": [
            {"contact_name": "Petra", "customer_name": "Zákazník", "url": "https://x.test/c"}
        ],
    }
    for template in ("activation_invite_customer", "activation_contact_no_login"):
        for locale in (None, "cs", "en", "de"):
            rendered = render_email(template, ctx, locale=locale)
            assert rendered.subject.strip()
            assert "https://acme.example.test/app/customers" in rendered.text
            assert "https://acme.example.test/app/customers" in rendered.html
