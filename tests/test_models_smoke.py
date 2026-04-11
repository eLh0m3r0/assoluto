"""Smoke test: verify the ORM can create and query the Tenant model.

Uses an in-memory SQLite database for speed — no Postgres required for
basic model sanity. Tests that rely on Postgres-specific features (JSONB,
RLS, UUID columns with server defaults) live in integration tests that
only run when a real Postgres is available.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.tenant import Tenant


@pytest.fixture
async def sqlite_session() -> AsyncSession:  # type: ignore[misc]
    """Yield an in-memory SQLite async session with the schema created."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        yield session
    await engine.dispose()


async def test_create_and_query_tenant(sqlite_session: AsyncSession) -> None:
    tenant = Tenant(
        id=uuid4(),
        slug="4mex",
        name="4MEX s.r.o.",
        billing_email="billing@4mex.cz",
        storage_prefix="tenants/4mex/",
    )
    sqlite_session.add(tenant)
    await sqlite_session.commit()

    result = await sqlite_session.execute(select(Tenant).where(Tenant.slug == "4mex"))
    loaded = result.scalar_one()

    assert loaded.name == "4MEX s.r.o."
    assert loaded.billing_email == "billing@4mex.cz"
    assert loaded.next_order_seq == 0
    assert loaded.is_active is True
    assert loaded.settings == {}
    assert loaded.created_at is not None
    assert loaded.updated_at is not None


async def test_tenant_slug_is_unique(sqlite_session: AsyncSession) -> None:
    sqlite_session.add(
        Tenant(
            id=uuid4(),
            slug="same",
            name="A",
            billing_email="a@example.com",
            storage_prefix="tenants/a/",
        )
    )
    await sqlite_session.commit()

    sqlite_session.add(
        Tenant(
            id=uuid4(),
            slug="same",
            name="B",
            billing_email="b@example.com",
            storage_prefix="tenants/b/",
        )
    )
    with pytest.raises(IntegrityError):
        await sqlite_session.commit()
