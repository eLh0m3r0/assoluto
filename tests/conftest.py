"""Shared pytest fixtures for the SME Client Portal tests."""

from __future__ import annotations

import os
import socket
from collections.abc import AsyncIterator
from urllib.parse import urlparse

import pytest
from httpx import ASGITransport, AsyncClient

# Set test environment BEFORE importing the app factory so that settings are
# populated correctly on first use.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_DEBUG", "false")
os.environ.setdefault("APP_SECRET_KEY", "test-secret-key")
os.environ.setdefault("LOG_LEVEL", "WARNING")

from app.config import Settings, get_settings
from app.main import create_app


@pytest.fixture(autouse=True)
def _reset_app_caches() -> None:
    """Clear cached Settings/engine/sessionmaker between tests.

    pytest-asyncio creates a fresh event loop for each test; an engine
    bound to a previous loop raises `RuntimeError: Event loop is closed`
    when it is later disposed, so we must rebuild it every test.
    """
    from app.db import session as db_session

    get_settings.cache_clear()
    db_session.get_engine.cache_clear()
    db_session.get_sessionmaker.cache_clear()


@pytest.fixture
def settings() -> Settings:
    """Return a fresh Settings instance for the test session."""
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    """ASGI in-process httpx client bound to a fresh app instance."""
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Postgres-backed integration test infrastructure
# ---------------------------------------------------------------------------

OWNER_URL_DEFAULT = "postgresql+asyncpg://portal:portal@localhost:5432/portal"
APP_URL_DEFAULT = "postgresql+asyncpg://portal_app:portal_app@localhost:5432/portal"


@pytest.fixture
async def owner_engine():  # type: ignore[misc]
    """Async engine running as the table owner — bypasses RLS.

    Use this in fixtures that need to seed or wipe data across tenants.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(os.environ.get("DATABASE_OWNER_URL", OWNER_URL_DEFAULT), future=True)
    yield eng
    await eng.dispose()


@pytest.fixture
async def wipe_db(owner_engine):  # type: ignore[misc]
    """Delete all tenant data before the test (and after, for safety)."""
    from sqlalchemy import text

    async def _wipe() -> None:
        async with owner_engine.begin() as conn:
            # Order matters because of FK constraints. Children first, then
            # parents; `customers` is RESTRICTed by `orders`, so orders (and
            # their dependents) must go first.
            await conn.execute(text("DELETE FROM order_comments"))
            await conn.execute(text("DELETE FROM order_status_history"))
            await conn.execute(text("DELETE FROM order_items"))
            await conn.execute(text("DELETE FROM orders"))
            await conn.execute(text("DELETE FROM customer_contacts"))
            await conn.execute(text("DELETE FROM customers"))
            await conn.execute(text("DELETE FROM users"))
            await conn.execute(text("DELETE FROM tenants"))

    await _wipe()
    yield
    await _wipe()


@pytest.fixture
async def demo_tenant(owner_engine, wipe_db):  # type: ignore[misc]
    """Create a single demo tenant named `4mex` and return it."""
    from uuid import uuid4

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.tenant import Tenant

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        tenant = Tenant(
            id=uuid4(),
            slug="4mex",
            name="4MEX s.r.o.",
            billing_email="billing@4mex.cz",
            storage_prefix="tenants/4mex/",
        )
        session.add(tenant)
        await session.flush()
        tenant_id = tenant.id
        tenant_slug = tenant.slug
        tenant_name = tenant.name

    # Return a lightweight record the tests can use without holding an
    # open DB session/transaction.
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class TenantRef:
        id: object
        slug: str
        name: str

    return TenantRef(id=tenant_id, slug=tenant_slug, name=tenant_name)


@pytest.fixture
async def tenant_client(settings: Settings, demo_tenant) -> AsyncIterator[AsyncClient]:
    """Like `client` but every request carries `X-Tenant-Slug: 4mex`.

    The underlying app uses the real portal_app DB user, so RLS is active
    on every query.
    """
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"X-Tenant-Slug": demo_tenant.slug},
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Postgres integration test helpers
# ---------------------------------------------------------------------------


def _postgres_reachable(url: str) -> bool:
    """Return True if a TCP connection to the Postgres host/port succeeds."""
    try:
        parsed = urlparse(url.replace("postgresql+asyncpg://", "postgresql://"))
    except Exception:
        return False
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def pytest_collection_modifyitems(config, items) -> None:
    """Skip tests marked `postgres` when a Postgres instance isn't reachable."""
    url = os.environ.get("DATABASE_URL", "postgresql+asyncpg://portal:portal@localhost:5432/portal")
    if _postgres_reachable(url):
        return
    skip_pg = pytest.mark.skip(reason="postgres not reachable (set DATABASE_URL)")
    for item in items:
        if "postgres" in item.keywords:
            item.add_marker(skip_pg)


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "postgres: test requires a running PostgreSQL at DATABASE_URL",
    )
