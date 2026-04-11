"""Async SQLAlchemy engine and session factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings, get_settings


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """Return a process-wide async engine.

    Cached so we reuse a single connection pool. Tests can clear the cache
    via `get_engine.cache_clear()` if they need a fresh engine.
    """
    settings: Settings = get_settings()
    return create_async_engine(
        settings.database_url,
        echo=False,
        future=True,
        pool_pre_ping=True,
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return a cached `async_sessionmaker` bound to the engine."""
    return async_sessionmaker(
        bind=get_engine(),
        expire_on_commit=False,
        class_=AsyncSession,
    )


async def session_scope() -> AsyncIterator[AsyncSession]:
    """Provide an `AsyncSession` in a context-managed block.

    Mostly used by scripts and background tasks. FastAPI request handlers
    should depend on `app.deps.get_db` instead so they get tenant-scoped
    sessions with RLS context set.
    """
    sm = get_sessionmaker()
    async with sm() as session:
        yield session
