"""CSRF protection tests.

These tests use the raw `httpx.AsyncClient` (not the CSRF-aware wrapper)
because we need to reason about the presence and absence of the token
explicitly.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.security.csrf import CSRF_COOKIE_NAME

pytestmark = pytest.mark.postgres


@pytest.fixture
async def raw_client(settings, demo_tenant):  # type: ignore[misc]
    """A vanilla AsyncClient that does NOT auto-inject CSRF tokens."""
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"X-Tenant-Slug": demo_tenant.slug},
    ) as ac:
        yield ac


async def test_post_without_csrf_token_returns_403(raw_client: AsyncClient) -> None:
    resp = await raw_client.post(
        "/auth/login",
        data={"email": "x@y.cz", "password": "whatever"},
    )
    assert resp.status_code == 403
    assert "CSRF" in resp.text


async def test_get_issues_csrf_cookie_and_post_with_it_proceeds(
    raw_client: AsyncClient,
) -> None:
    """A normal browser-style flow: GET a form, harvest the cookie, POST."""
    get = await raw_client.get("/auth/login")
    assert get.status_code == 200
    token = raw_client.cookies.get(CSRF_COOKIE_NAME)
    assert token, "csrftoken cookie should be set on first GET"

    # The hidden input should contain the same token.
    assert f'value="{token}"' in get.text

    # Submitting with the token but wrong credentials now reaches auth
    # logic (401) rather than being blocked by CSRF (403).
    resp = await raw_client.post(
        "/auth/login",
        data={"email": "x@y.cz", "password": "whatever", "csrf_token": token},
    )
    assert resp.status_code == 401


async def test_csrf_header_works_for_json_clients(raw_client: AsyncClient) -> None:
    """API clients that don't use forms can set X-CSRF-Token instead."""
    await raw_client.get("/auth/login")  # seed the cookie
    token = raw_client.cookies.get(CSRF_COOKIE_NAME)
    assert token

    resp = await raw_client.post(
        "/auth/login",
        data={"email": "x@y.cz", "password": "whatever"},
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 401  # auth fails, not CSRF


async def test_csrf_mismatch_returns_403(raw_client: AsyncClient) -> None:
    await raw_client.get("/auth/login")  # seed cookie
    resp = await raw_client.post(
        "/auth/login",
        data={
            "email": "x@y.cz",
            "password": "whatever",
            "csrf_token": "not-the-right-one",
        },
    )
    assert resp.status_code == 403


async def test_safe_methods_never_require_csrf(raw_client: AsyncClient) -> None:
    for path in ("/healthz", "/readyz", "/auth/login", "/"):
        resp = await raw_client.get(path)
        assert resp.status_code in (200, 404)  # /  -> 404 if no tenant, 200 otherwise
