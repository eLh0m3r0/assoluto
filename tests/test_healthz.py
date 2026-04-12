"""Smoke tests for health probes and the tenant landing page."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


async def test_healthz_returns_ok(client: AsyncClient) -> None:
    """Liveness probe must work WITHOUT a tenant context."""
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_root_without_tenant_is_404(client: AsyncClient) -> None:
    """Without a tenant header or subdomain, `/` returns 404."""
    response = await client.get("/")
    assert response.status_code == 404


@pytest.mark.postgres
async def test_root_returns_html_landing_for_tenant(
    tenant_client: AsyncClient,
) -> None:
    response = await tenant_client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "SME Client Portal" in body
    assert "Zákaznický portál" in body
