"""Shared FastAPI dependencies for the platform package."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings, get_settings
from app.platform.models import Identity
from app.platform.session import read_platform_session

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_owner_engine(settings: Settings) -> AsyncEngine:
    """Return a lazily-initialised owner engine for platform DB work."""
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_async_engine(settings.database_owner_url, future=True, pool_pre_ping=True)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def reset_platform_engine() -> None:
    """Drop the cached engine + sessionmaker (test-only)."""
    global _engine, _sessionmaker
    _engine = None
    _sessionmaker = None


async def get_platform_db(
    settings: Settings = Depends(get_settings),
) -> AsyncIterator[AsyncSession]:
    """Yield an owner-scoped session for platform operations.

    NOTE: this session BYPASSES RLS because platform flows must see
    data across all tenants.
    """
    get_owner_engine(settings)
    assert _sessionmaker is not None  # set by get_owner_engine
    async with _sessionmaker() as session:
        yield session


async def get_current_identity(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_platform_db),
) -> Identity | None:
    """Resolve the caller from the `sme_portal_platform` cookie, or None."""
    from sqlalchemy import select

    session_data = read_platform_session(request, settings.app_secret_key)
    if session_data is None:
        return None
    try:
        identity_uuid = UUID(session_data.identity_id)
    except ValueError:
        return None
    identity = (
        await db.execute(select(Identity).where(Identity.id == identity_uuid))
    ).scalar_one_or_none()
    if identity is None or not identity.is_active:
        return None
    return identity


async def require_identity(
    identity: Identity | None = Depends(get_current_identity),
) -> Identity:
    if identity is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return identity


async def require_platform_admin(
    identity: Identity = Depends(require_identity),
) -> Identity:
    if not identity.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin required")
    return identity
