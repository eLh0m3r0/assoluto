"""Migration ``1011_pricing_2026_10`` — CEO decision D3 (2026-10-04).

Starter 1 490 CZK / 3 staff / 10 GB, Pro 2 990 CZK / 10 staff / 50 GB,
client contacts and orders unlimited (NULL) on both. The downgrade must
restore the 1003 seed values exactly.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.postgres

_MIGRATION = (
    Path(__file__).resolve().parent.parent / "migrations" / "versions" / "1011_pricing_2026_10.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig_1011_pricing", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SELECT = text(
    "SELECT code, monthly_price_cents, max_users, max_contacts, "
    "max_orders_per_month, max_storage_mb FROM platform_plans "
    "WHERE code IN ('starter', 'pro') ORDER BY code"
)


async def test_plans_carry_the_2026_10_prices_and_limits(owner_engine) -> None:
    async with owner_engine.connect() as conn:
        rows = {r.code: tuple(r) for r in (await conn.execute(_SELECT)).all()}
    assert rows["starter"] == ("starter", 149000, 3, None, None, 10240)
    assert rows["pro"] == ("pro", 299000, 10, None, None, 51200)


async def test_downgrade_restores_previous_values_and_upgrade_reapplies(owner_engine) -> None:
    """Run the migration's own downgrade/upgrade inside a rolled-back
    transaction so the shared test DB is left untouched."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    module = _load_migration()

    def _roundtrip(sync_conn):
        ctx = MigrationContext.configure(sync_conn)
        with Operations.context(ctx):
            module.downgrade()
            after_down = {r.code: tuple(r) for r in sync_conn.execute(_SELECT).all()}
            module.upgrade()
            after_up = {r.code: tuple(r) for r in sync_conn.execute(_SELECT).all()}
        return after_down, after_up

    async with owner_engine.connect() as conn:
        trans = await conn.begin()
        try:
            after_down, after_up = await conn.run_sync(_roundtrip)
        finally:
            await trans.rollback()

    assert after_down["starter"] == ("starter", 49000, 3, 20, None, 2048)
    assert after_down["pro"] == ("pro", 149000, 15, 100, None, 20480)
    assert after_up["starter"] == ("starter", 149000, 3, None, None, 10240)
    assert after_up["pro"] == ("pro", 299000, 10, None, None, 51200)


async def test_ensure_within_limit_lets_starter_invite_many_contacts(owner_engine, wipe_db) -> None:
    """Contacts are no longer metered: a Starter tenant far past the old
    cap of 20 contacts must not hit PlanLimitExceeded."""
    from uuid import uuid4

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.customer import Customer, CustomerContact
    from app.models.tenant import Tenant
    from app.platform.billing.models import Plan, Subscription
    from app.platform.usage import ensure_within_limit

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        tenant = Tenant(
            id=uuid4(),
            slug=f"ctc-{uuid4().hex[:6]}",
            name="Contacts Co",
            billing_email="b@contacts.example.com",
            storage_prefix="tenants/ctc/",
        )
        session.add(tenant)
        await session.flush()
        starter = (await session.execute(select(Plan).where(Plan.code == "starter"))).scalar_one()
        session.add(Subscription(tenant_id=tenant.id, plan_id=starter.id, status="active"))
        customer = Customer(id=uuid4(), tenant_id=tenant.id, name="Client")
        session.add(customer)
        await session.flush()
        for i in range(25):
            session.add(
                CustomerContact(
                    id=uuid4(),
                    tenant_id=tenant.id,
                    customer_id=customer.id,
                    email=f"c{i}@client.example.com",
                    full_name=f"Contact {i}",
                )
            )

    async with sm() as session:
        await ensure_within_limit(session, tenant_id=tenant.id, metric="contacts")
