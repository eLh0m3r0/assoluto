"""Regression tests for the 2026-07-26 audit — billing lifecycle.

See docs/audit-runs/2026-07-26-e2e/findings.md.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.tenant import Tenant
from app.platform.billing.models import Plan, Subscription
from app.platform.billing.webhooks import (
    handle_subscription_deleted,
    handle_subscription_upserted,
)

pytestmark = pytest.mark.postgres


async def _seed_subscription(owner_engine, *, status: str, tenant_active: bool = True):
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        tenant = Tenant(
            id=uuid4(),
            slug=f"billing-{uuid4().hex[:8]}",
            name="Billing Co",
            billing_email=f"billing-{uuid4().hex[:6]}@example.com",
            storage_prefix=f"billing-{uuid4().hex[:8]}",
            is_active=tenant_active,
        )
        session.add(tenant)
        await session.flush()
        plan = (
            await session.execute(select(Plan).where(Plan.code == "starter"))
        ).scalar_one()
        sub = Subscription(
            id=uuid4(),
            tenant_id=tenant.id,
            plan_id=plan.id,
            status=status,
            stripe_subscription_id="sub_test_123",
            stripe_customer_id="cus_test_123",
            current_period_end=datetime.now(UTC),
        )
        session.add(sub)
        await session.flush()
        return tenant, sub


def _event(sub_id: str, tenant_id, status: str) -> dict:
    return {
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": sub_id,
                "customer": "cus_test_123",
                "status": status,
                "metadata": {"tenant_id": str(tenant_id)},
                "items": {"data": []},
            }
        },
    }


# --------------------------------------------------------------- F-09


async def test_f09_stale_update_cannot_resurrect_a_cancelled_subscription(
    owner_engine, demo_tenant
) -> None:
    """Stripe guarantees delivery, not ORDER.

    An ``updated`` event generated before the cancellation but delivered
    after ``deleted`` used to write status='active' straight over
    'canceled', reviving a subscription nobody pays for and cancelling
    the scheduled hard-cut.
    """
    tenant, _sub = await _seed_subscription(owner_engine, status="active")
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)

    async with sm() as session, session.begin():
        await handle_subscription_deleted(
            session,
            {
                "type": "customer.subscription.deleted",
                "data": {
                    "object": {
                        "id": "sub_test_123",
                        "customer": "cus_test_123",
                        "metadata": {"tenant_id": str(tenant.id)},
                    }
                },
            },
        )

    # The late-arriving stale update for the SAME subscription.
    async with sm() as session, session.begin():
        await handle_subscription_upserted(
            session, _event("sub_test_123", tenant.id, "active")
        )

    async with sm() as session:
        got = (
            await session.execute(select(Subscription).where(Subscription.tenant_id == tenant.id))
        ).scalar_one()
        assert got.status == "canceled", "a stale update must not revive a cancelled sub"


# --------------------------------------------------------------- F-08


async def test_f08_resubscribing_reactivates_a_hard_cut_tenant(owner_engine) -> None:
    """enforce_canceled_subscriptions sets tenants.is_active=false and
    nothing ever set it back, so a customer who paid again still could
    not log in.
    """
    tenant, _sub = await _seed_subscription(
        owner_engine, status="canceled", tenant_active=False
    )
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)

    # A genuinely new subscription — different Stripe id, so it is not
    # the stale-update case guarded above.
    async with sm() as session, session.begin():
        await handle_subscription_upserted(
            session, _event("sub_test_NEW", tenant.id, "active")
        )

    async with sm() as session:
        got = (await session.execute(select(Tenant).where(Tenant.id == tenant.id))).scalar_one()
        assert got.is_active is True, "paying again must undo the hard cut"


# --------------------------------------------------------------- trial plan


async def test_signup_trial_uses_the_plan_the_visitor_clicked(owner_engine) -> None:
    """Clicking "Start 30-day trial" on the Pro card used to start a
    Starter trial capped at 3 users / 20 contacts.
    """
    from app.platform.service import signup_tenant

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    slug = f"protrial-{uuid4().hex[:8]}"
    async with sm() as session, session.begin():
        tenant, _owner, _identity = await signup_tenant(
            session,
            company_name="Pro Trial Co",
            slug=slug,
            owner_email=f"pro-{uuid4().hex[:8]}@example.com",
            owner_full_name="Pro Owner",
            owner_password="averysecret1",
            plan_code="pro",
        )
        tenant_id = tenant.id

    async with sm() as session:
        sub = (
            await session.execute(
                select(Subscription).where(Subscription.tenant_id == tenant_id)
            )
        ).scalar_one()
        plan = (await session.execute(select(Plan).where(Plan.id == sub.plan_id))).scalar_one()
        assert plan.code == "pro", "the trial must run on the plan that was clicked"
