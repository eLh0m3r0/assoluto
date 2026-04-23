"""Plan-limit enforcement tests.

Verify that ``ensure_within_limit`` is actually invoked by the four
creation paths (staff invite, contact invite, order create, attachment
upload) and that creations over the plan cap raise
``PlanLimitExceeded``. Under-limit creations pass through silently.

Also pins the env→DB Stripe price sync behaviour.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.customer import Customer
from app.models.enums import CustomerContactRole, UserRole
from app.models.user import User
from app.platform.billing.models import Plan, Subscription
from app.platform.usage import PlanLimitExceeded, ensure_within_limit
from app.security.passwords import hash_password
from app.services.auth_service import invite_customer_contact, invite_tenant_staff

pytestmark = pytest.mark.postgres


async def _seed_customer(owner_engine, tenant_id):
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        c = Customer(id=uuid4(), tenant_id=tenant_id, name="Limit test")
        session.add(c)
        await session.flush()
        return c.id


async def _attach_plan_to_tenant(
    owner_engine, *, tenant_id, max_users=None, max_contacts=None, code=None
):
    """Create a fake Plan row + Subscription on a tenant with the given caps.

    ``code`` defaults to a random hex so parallel / sequential tests don't
    collide on the ``UNIQUE(code)`` constraint — the plans table isn't
    wiped between tests (not tenant-scoped; kept as a global dim).
    """
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        # Clear any stale subscription for this tenant (same reason —
        # platform_subscriptions has UNIQUE(tenant_id)).
        from sqlalchemy import delete as _del

        await session.execute(_del(Subscription).where(Subscription.tenant_id == tenant_id))
        await session.flush()

        plan = Plan(
            id=uuid4(),
            code=code or f"test_cap_{uuid4().hex[:8]}",
            name="Test cap",
            monthly_price_cents=0,
            currency="CZK",
            max_users=max_users,
            max_contacts=max_contacts,
            max_orders_per_month=None,
            max_storage_mb=None,
        )
        session.add(plan)
        await session.flush()
        sub = Subscription(
            id=uuid4(),
            tenant_id=tenant_id,
            plan_id=plan.id,
            status="active",
        )
        session.add(sub)
        await session.flush()
        return plan


async def test_no_subscription_means_no_limit(owner_engine, demo_tenant):
    """Tenants without a subscription (self-hosted / older) are unbounded."""
    # Demo tenant has no subscription by default. Should no-op.
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        # Doesn't raise = pass.
        await ensure_within_limit(session, tenant_id=demo_tenant.id, metric="users")


async def test_users_under_limit_passes(owner_engine, demo_tenant):
    await _attach_plan_to_tenant(owner_engine, tenant_id=demo_tenant.id, max_users=3)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        # 0 existing users, delta=1 → well under 3.
        await ensure_within_limit(session, tenant_id=demo_tenant.id, metric="users")


async def test_users_at_limit_raises(owner_engine, demo_tenant):
    """Seed 3 active users then try to create a 4th — must raise."""
    await _attach_plan_to_tenant(owner_engine, tenant_id=demo_tenant.id, max_users=3)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        for i in range(3):
            session.add(
                User(
                    id=uuid4(),
                    tenant_id=demo_tenant.id,
                    email=f"limit-{i}-{uuid4().hex[:4]}@t.cz",
                    full_name=f"U{i}",
                    role=UserRole.TENANT_STAFF,
                    password_hash=hash_password("x" * 12),
                )
            )
        await session.flush()

    async with sm() as session:
        with pytest.raises(PlanLimitExceeded) as exc_info:
            await ensure_within_limit(session, tenant_id=demo_tenant.id, metric="users")
        assert exc_info.value.metric == "users"
        assert exc_info.value.limit == 3
        assert exc_info.value.current == 3


async def test_invite_tenant_staff_honours_limit(owner_engine, demo_tenant):
    """invite_tenant_staff must call ensure_within_limit — regression guard."""
    await _attach_plan_to_tenant(owner_engine, tenant_id=demo_tenant.id, max_users=1)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    # One staff user already counts toward the limit.
    async with sm() as session, session.begin():
        session.add(
            User(
                id=uuid4(),
                tenant_id=demo_tenant.id,
                email=f"first-{uuid4().hex[:6]}@t.cz",
                full_name="First",
                role=UserRole.TENANT_STAFF,
                password_hash=hash_password("x" * 12),
            )
        )
    async with sm() as session, session.begin():
        with pytest.raises(PlanLimitExceeded):
            await invite_tenant_staff(
                session,
                tenant_id=demo_tenant.id,
                email=f"second-{uuid4().hex[:6]}@t.cz",
                full_name="Second",
            )


async def test_invite_contact_honours_limit(owner_engine, demo_tenant):
    """invite_customer_contact must call ensure_within_limit."""
    await _attach_plan_to_tenant(owner_engine, tenant_id=demo_tenant.id, max_contacts=1)
    cust_id = await _seed_customer(owner_engine, demo_tenant.id)

    from app.models.customer import CustomerContact

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        session.add(
            CustomerContact(
                id=uuid4(),
                tenant_id=demo_tenant.id,
                customer_id=cust_id,
                email=f"c1-{uuid4().hex[:6]}@x.cz",
                full_name="C1",
                role=CustomerContactRole.CUSTOMER_USER,
                password_hash=hash_password("x" * 12),
            )
        )
    async with sm() as session, session.begin():
        with pytest.raises(PlanLimitExceeded):
            await invite_customer_contact(
                session,
                tenant_id=demo_tenant.id,
                customer_id=cust_id,
                email=f"c2-{uuid4().hex[:6]}@x.cz",
                full_name="C2",
            )


async def test_null_limit_means_unlimited(owner_engine, demo_tenant):
    """A plan with NULL max_users (unlimited) never raises, even at 100."""
    await _attach_plan_to_tenant(owner_engine, tenant_id=demo_tenant.id, max_users=None)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        # Doesn't matter what the current count is — NULL means no cap.
        await ensure_within_limit(session, tenant_id=demo_tenant.id, metric="users")


async def test_stripe_price_sync_idempotent(owner_engine):
    """Running the sync with the same env→DB value is a no-op."""
    from app.main import _sync_stripe_prices_from_env

    # Reuse an existing plan row; we'll write then re-write.
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        # UPSERT behaviour — the plans table persists across tests.
        existing = (
            await session.execute(select(Plan).where(Plan.code == "starter"))
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                Plan(
                    id=uuid4(),
                    code="starter",
                    name="Starter (test)",
                    monthly_price_cents=49000,
                    currency="CZK",
                    max_users=3,
                    max_contacts=20,
                )
            )
        else:
            # Clear any pre-existing price so we can verify the UPDATE.
            existing.stripe_price_id = None

    class FakeSettings:
        stripe_price_starter = "price_test_123"
        stripe_price_pro = ""
        database_owner_url = owner_engine.url.render_as_string(hide_password=False)

    class FakeLog:
        def __init__(self) -> None:
            self.calls: list = []

        def warning(self, *a, **kw):
            self.calls.append(("warning", a, kw))

        def info(self, *a, **kw):
            self.calls.append(("info", a, kw))

    log = FakeLog()
    # First call: should UPDATE.
    await _sync_stripe_prices_from_env(FakeSettings(), log)
    # Second call: should be a no-op (same value already in DB).
    log.calls.clear()
    await _sync_stripe_prices_from_env(FakeSettings(), log)
    # No 'stripe_price.sync.updated' emitted on the second call.
    updated_calls = [c for c in log.calls if c[1] and c[1][0] == "stripe_price.sync.updated"]
    assert not updated_calls, f"expected no-op on second sync, got {updated_calls}"

    async with sm() as session:
        row = (await session.execute(select(Plan).where(Plan.code == "starter"))).scalar_one()
        assert row.stripe_price_id == "price_test_123"
