"""Option A — cancel = real cancel + 3-day grace + hard cut.

Covers:
* ``cancel_subscription`` (demo path) — status flips to canceled,
  current_period_end stamped to now, grace_ends = period_end + 3d.
* ``enforce_canceled_subscriptions`` periodic job — deactivates tenants
  past grace, kicks every user/contact session, idempotent on re-run.
* ``list_plans`` — Community is filtered out of the hosted plan list
  (HIDDEN_PLAN_CODES).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.models.customer import Customer, CustomerContact
from app.models.enums import UserRole
from app.models.tenant import Tenant
from app.models.user import User
from app.platform.billing.models import Plan, Subscription
from app.platform.billing.service import (
    HIDDEN_PLAN_CODES,
    cancel_subscription,
    list_plans,
)
from app.security.passwords import hash_password
from app.tasks.periodic import (
    CANCEL_GRACE_DAYS,
    enforce_canceled_subscriptions,
)

pytestmark = pytest.mark.postgres


async def _seed_tenant_with_subscription(
    owner_engine,
    *,
    slug: str,
    period_end: datetime | None = None,
    status: str = "active",
) -> tuple[Tenant, Subscription]:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        tenant = Tenant(
            id=uuid4(),
            slug=slug,
            name=slug.title(),
            billing_email=f"o@{slug}.cz",
            storage_prefix=f"tenants/{slug}/",
        )
        session.add(tenant)
        await session.flush()
        starter = (await session.execute(select(Plan).where(Plan.code == "starter"))).scalar_one()
        sub = Subscription(
            tenant_id=tenant.id,
            plan_id=starter.id,
            status=status,
            current_period_end=period_end,
        )
        session.add(sub)
    async with sm() as session:
        tenant = (await session.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one()
        sub = (
            await session.execute(select(Subscription).where(Subscription.tenant_id == tenant.id))
        ).scalar_one()
    return tenant, sub


async def test_cancel_subscription_demo_flips_locally(owner_engine, wipe_db) -> None:
    """Demo mode (no Stripe): immediate flip — status=canceled,
    current_period_end = now (so grace starts now), plan_id stays.
    """
    _tenant, sub = await _seed_tenant_with_subscription(
        owner_engine, slug="cancel-demo", period_end=None
    )
    original_plan_id = sub.plan_id

    settings = get_settings()
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        sub_attached = (
            await session.execute(select(Subscription).where(Subscription.id == sub.id))
        ).scalar_one()
        outcome, access_ends_at = await cancel_subscription(
            session, settings, subscription=sub_attached
        )
        await session.commit()

    assert outcome == "flipped"
    assert access_ends_at is not None
    # access_ends_at == period_end + grace; period_end was stamped to now.
    assert access_ends_at - datetime.now(UTC) <= timedelta(days=CANCEL_GRACE_DAYS, hours=1)

    async with sm() as session:
        sub_fresh = (
            await session.execute(select(Subscription).where(Subscription.id == sub.id))
        ).scalar_one()
        assert sub_fresh.status == "canceled"
        assert sub_fresh.cancel_at_period_end is False
        assert sub_fresh.current_period_end is not None
        # plan_id NOT flipped — historical record of what they had.
        assert sub_fresh.plan_id == original_plan_id


async def test_cancel_subscription_preserves_existing_period_end(owner_engine, wipe_db) -> None:
    """If current_period_end is already set in the future (paid period
    not yet over), don't overwrite it — the user paid for it, they get
    to keep it. Grace runs from that natural period_end.
    """
    future_end = datetime.now(UTC) + timedelta(days=10)
    _tenant, sub = await _seed_tenant_with_subscription(
        owner_engine, slug="cancel-future", period_end=future_end
    )

    settings = get_settings()
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        sub_attached = (
            await session.execute(select(Subscription).where(Subscription.id == sub.id))
        ).scalar_one()
        outcome, access_ends_at = await cancel_subscription(
            session, settings, subscription=sub_attached
        )
        await session.commit()

    assert outcome == "flipped"
    assert access_ends_at is not None
    # access_ends should be ~future_end + 3 days, not now + 3 days.
    expected = future_end + timedelta(days=CANCEL_GRACE_DAYS)
    assert abs((access_ends_at - expected).total_seconds()) < 60

    async with sm() as session:
        sub_fresh = (
            await session.execute(select(Subscription).where(Subscription.id == sub.id))
        ).scalar_one()
        # period_end must be unchanged.
        assert abs((sub_fresh.current_period_end - future_end).total_seconds()) < 60


async def test_enforce_canceled_subscriptions_deactivates_after_grace(
    owner_engine, wipe_db
) -> None:
    """A canceled subscription whose period_end + grace is in the past
    triggers tenant deactivation: tenants.is_active=False AND
    session_version bumped on every user + contact in the tenant.
    """
    # period_end = 5 days ago → grace expired 2 days ago.
    past = datetime.now(UTC) - timedelta(days=5)
    tenant, _sub = await _seed_tenant_with_subscription(
        owner_engine, slug="enforce-cut", period_end=past, status="canceled"
    )

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        # Seed a user and a contact so we can verify session_version bump.
        customer = Customer(
            id=uuid4(),
            tenant_id=tenant.id,
            name="C",
        )
        session.add(customer)
        await session.flush()
        session.add(
            User(
                tenant_id=tenant.id,
                email="user@enforce-cut.cz",
                full_name="User",
                role=UserRole.TENANT_ADMIN,
                password_hash=hash_password("x" * 12),
            )
        )
        session.add(
            CustomerContact(
                tenant_id=tenant.id,
                customer_id=customer.id,
                email="contact@enforce-cut.cz",
                full_name="Contact",
                password_hash=hash_password("y" * 12),
                accepted_at=datetime.now(UTC),
            )
        )

    deactivated = await enforce_canceled_subscriptions()
    assert deactivated == 1

    async with sm() as session:
        t = (await session.execute(select(Tenant).where(Tenant.id == tenant.id))).scalar_one()
        assert t.is_active is False
        u = (
            await session.execute(select(User).where(User.email == "user@enforce-cut.cz"))
        ).scalar_one()
        assert u.session_version >= 1
        c = (
            await session.execute(
                select(CustomerContact).where(CustomerContact.email == "contact@enforce-cut.cz")
            )
        ).scalar_one()
        assert c.session_version >= 1


async def test_enforce_canceled_subscriptions_respects_grace(owner_engine, wipe_db) -> None:
    """A canceled subscription whose grace has NOT expired (period_end
    in the recent past, less than CANCEL_GRACE_DAYS ago) is left alone.
    """
    recent = datetime.now(UTC) - timedelta(days=1)  # 1 day ago, grace = 3
    tenant, _sub = await _seed_tenant_with_subscription(
        owner_engine, slug="enforce-keep", period_end=recent, status="canceled"
    )

    deactivated = await enforce_canceled_subscriptions()
    assert deactivated == 0

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        t = (await session.execute(select(Tenant).where(Tenant.id == tenant.id))).scalar_one()
        assert t.is_active is True


async def test_enforce_canceled_subscriptions_idempotent(owner_engine, wipe_db) -> None:
    """Re-running on an already-deactivated tenant is a no-op (count 0)."""
    past = datetime.now(UTC) - timedelta(days=5)
    _tenant, _sub = await _seed_tenant_with_subscription(
        owner_engine, slug="enforce-idem", period_end=past, status="canceled"
    )

    first = await enforce_canceled_subscriptions()
    assert first == 1

    second = await enforce_canceled_subscriptions()
    assert second == 0


async def test_cancel_subscription_writes_audit_row(owner_engine, wipe_db) -> None:
    """``cancel_subscription`` records an audit_events row in the tenant's
    audit log so the tenant admin can see who cancelled and when.
    """
    from app.models.audit_event import AuditEvent

    _tenant, sub = await _seed_tenant_with_subscription(
        owner_engine, slug="cancel-audited", period_end=None
    )
    settings = get_settings()
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        sub_attached = (
            await session.execute(select(Subscription).where(Subscription.id == sub.id))
        ).scalar_one()
        await cancel_subscription(
            session,
            settings,
            subscription=sub_attached,
            actor_label="ops@assoluto.eu",
        )
        await session.commit()

    async with sm() as session:
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.action == "billing.subscription_canceled")
                )
            )
            .scalars()
            .all()
        )

    assert len(events) == 1
    event = events[0]
    assert event.entity_type == "subscription"
    assert event.entity_id == sub.id
    assert event.actor_label == "ops@assoluto.eu"
    assert event.diff is not None
    assert event.diff.get("after", {}).get("mode") == "demo"


async def test_list_plans_excludes_community(owner_engine, wipe_db) -> None:
    """Community is hidden from the hosted plan list — it's the AGPL
    self-host pitch on /pricing, not a hosted choice. HIDDEN_PLAN_CODES
    enforces this server-side.
    """
    assert "community" in HIDDEN_PLAN_CODES

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        plans = await list_plans(session)

    codes = {p.code for p in plans}
    assert "community" not in codes
    # The other plans are still there (they're seeded by migration 1003).
    assert "starter" in codes
    assert "pro" in codes
    assert "enterprise" in codes
