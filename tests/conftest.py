"""Shared pytest fixtures for the SME Client Portal tests."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

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
