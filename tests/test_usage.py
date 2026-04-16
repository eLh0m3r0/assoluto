"""Usage metering + plan limit enforcement tests."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.customer import Customer, CustomerContact
from app.models.enums import OrderStatus, UserRole
from app.models.order import Order
from app.models.tenant import Tenant
from app.models.user import User
from app.platform.billing.models import Plan, Subscription
from app.platform.usage import PlanLimitExceeded, ensure_within_limit, snapshot_tenant_usage
from app.security.passwords import hash_password

pytestmark = pytest.mark.postgres


async def _make_tenant(session, slug: str = "u-test") -> Tenant:
    tenant = Tenant(
        id=uuid4(),
        slug=slug,
        name="UsageTest",
        billing_email="b@u.cz",
        storage_prefix=f"tenants/{slug}/",
    )
    session.add(tenant)
    await session.flush()
    return tenant


async def _make_user(session, tenant: Tenant, email: str) -> User:
    u = User(
        id=uuid4(),
        tenant_id=tenant.id,
        email=email,
        full_name="U",
        password_hash=hash_password("x" * 10),
        role=UserRole.TENANT_STAFF,
    )
    session.add(u)
    await session.flush()
    return u


async def _make_contact(session, tenant: Tenant, email: str) -> CustomerContact:
    customer = Customer(id=uuid4(), tenant_id=tenant.id, name="C")
    session.add(customer)
    await session.flush()
    contact = CustomerContact(
        id=uuid4(),
        tenant_id=tenant.id,
        customer_id=customer.id,
        email=email,
        full_name="CC",
    )
    session.add(contact)
    await session.flush()
    return contact


async def test_snapshot_counts_users_contacts_orders(owner_engine, wipe_db) -> None:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        tenant = await _make_tenant(session)
        await _make_user(session, tenant, "a@u.cz")
        await _make_user(session, tenant, "b@u.cz")
        await _make_contact(session, tenant, "c@u.cz")

        order = Order(
            id=uuid4(),
            tenant_id=tenant.id,
            customer_id=(
                await session.execute(select(Customer).where(Customer.tenant_id == tenant.id))
            )
            .scalar_one()
            .id,
            number="2026-000001",
            title="T",
            status=OrderStatus.DRAFT,
        )
        session.add(order)
        await session.flush()

    async with sm() as session:
        snap = await snapshot_tenant_usage(session, tenant.id)
        assert snap.users == 2
        assert snap.contacts == 1
        assert snap.orders_this_month == 1
        assert snap.storage_bytes == 0


async def test_ensure_within_limit_without_subscription_is_noop(owner_engine, wipe_db) -> None:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        tenant = await _make_tenant(session, slug="nosubs")

    async with sm() as session:
        # No PlanLimitExceeded: no subscription means no limits.
        await ensure_within_limit(session, tenant_id=tenant.id, metric="users")


async def test_ensure_within_limit_raises_on_starter_user_cap(owner_engine, wipe_db) -> None:
    """Starter plan caps users at 3 — the 4th user creation must be blocked."""
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        tenant = await _make_tenant(session, slug="atcap")
        # Seed Starter plan is already in DB via migration.
        starter = (await session.execute(select(Plan).where(Plan.code == "starter"))).scalar_one()
        sub = Subscription(
            tenant_id=tenant.id,
            plan_id=starter.id,
            status="active",
        )
        session.add(sub)
        # 3 existing users (at cap).
        for i in range(3):
            await _make_user(session, tenant, f"u{i}@atcap.cz")

    async with sm() as session:
        # 4th user would exceed cap.
        with pytest.raises(PlanLimitExceeded) as excinfo:
            await ensure_within_limit(session, tenant_id=tenant.id, metric="users")
        assert excinfo.value.metric == "users"
        assert excinfo.value.limit == 3
        assert excinfo.value.current == 3


async def test_ensure_within_limit_community_is_unlimited(owner_engine, wipe_db) -> None:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        tenant = await _make_tenant(session, slug="uncapped")
        community = (
            await session.execute(select(Plan).where(Plan.code == "community"))
        ).scalar_one()
        session.add(Subscription(tenant_id=tenant.id, plan_id=community.id, status="active"))
        # Plenty of users.
        for i in range(10):
            await _make_user(session, tenant, f"u{i}@uncap.cz")

    async with sm() as session:
        # Still no error — community has NULL max_users.
        await ensure_within_limit(session, tenant_id=tenant.id, metric="users")


def test_snapshot_percent_of() -> None:
    from app.platform.usage import UsageSnapshot

    snap = UsageSnapshot(
        users=2, contacts=10, orders_this_month=50, storage_bytes=1024 * 1024 * 512
    )
    # 2/3 users ≈ 66%
    assert snap.percent_of("users", 3) == 66
    # 50/100 orders = 50%
    assert snap.percent_of("orders", 100) == 50
    # 512 MB / 2048 MB = 25%
    assert snap.percent_of("storage_mb", 2048) == 25
    # Unlimited returns 0
    assert snap.percent_of("users", None) == 0
