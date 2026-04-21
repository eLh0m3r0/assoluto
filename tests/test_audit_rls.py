"""Cross-tenant read isolation for ``audit_events``.

``audit_events`` is RLS-protected like every other tenant-scoped table.
Sessions with a different ``app.tenant_id`` must not see rows belonging
to another tenant.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.audit_event import AuditEvent
from app.models.tenant import Tenant

pytestmark = pytest.mark.postgres


APP_URL_DEFAULT = "postgresql+asyncpg://portal_app:portal_app@localhost:5432/portal"
OWNER_URL_DEFAULT = "postgresql+asyncpg://portal:portal@localhost:5432/portal"


@pytest.fixture
async def app_engine():  # type: ignore[misc]
    eng = create_async_engine(os.environ.get("DATABASE_URL", APP_URL_DEFAULT), future=True)
    yield eng
    await eng.dispose()


@pytest.fixture
async def two_tenants_seeded_audit(owner_engine, wipe_db):  # type: ignore[misc]
    """Create two tenants, each with a single audit_events row."""
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)

    tenants: dict[str, Tenant] = {}
    events: dict[str, AuditEvent] = {}

    async with sm() as session, session.begin():
        for slug in ("alpha", "beta"):
            tenant = Tenant(
                id=uuid4(),
                slug=slug,
                name=f"{slug} s.r.o.",
                billing_email=f"billing@{slug}.cz",
                storage_prefix=f"tenants/{slug}/",
            )
            session.add(tenant)
            await session.flush()
            tenants[slug] = tenant

            ev = AuditEvent(
                id=uuid4(),
                tenant_id=tenant.id,
                occurred_at=datetime.now(UTC),
                actor_type="system",
                actor_id=None,
                actor_label="system",
                action=f"{slug}.test",
                entity_type="order",
                entity_id=uuid4(),
                entity_label=f"{slug}-label",
            )
            session.add(ev)
            events[slug] = ev

    return tenants, events


async def test_audit_events_isolated_between_tenants(app_engine, two_tenants_seeded_audit) -> None:
    tenants, events = two_tenants_seeded_audit
    sm = async_sessionmaker(app_engine, expire_on_commit=False)

    # alpha sees only alpha's event.
    async with sm() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": str(tenants["alpha"].id)},
        )
        rows = (await session.execute(select(AuditEvent))).scalars().all()
        assert len(rows) == 1
        assert rows[0].id == events["alpha"].id

    # beta sees only beta's event.
    async with sm() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": str(tenants["beta"].id)},
        )
        rows = (await session.execute(select(AuditEvent))).scalars().all()
        assert len(rows) == 1
        assert rows[0].id == events["beta"].id


async def test_audit_events_without_tenant_sees_nothing(
    app_engine, two_tenants_seeded_audit
) -> None:
    sm = async_sessionmaker(app_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        rows = (await session.execute(select(AuditEvent))).scalars().all()
        assert rows == []
