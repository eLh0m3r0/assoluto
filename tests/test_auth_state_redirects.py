"""Authenticated visitors never see unauthenticated CTAs.

Regression tests for the "why is the login page open when I'm signed
in?" bug class documented in CLAUDE.md §8. Every GET that renders a
login / signup / password-reset form must redirect when a valid
session cookie is already present.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.enums import UserRole
from app.models.user import User
from app.security.passwords import hash_password

pytestmark = pytest.mark.postgres


async def _seed_admin(owner_engine, demo_tenant, *, password: str = "ownerpass-123") -> str:
    """Create a tenant_admin user with a known password and return the email."""
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    email = f"owner-{uuid4().hex[:6]}@4mex.cz"
    async with sm() as session, session.begin():
        session.add(
            User(
                tenant_id=demo_tenant.id,
                email=email,
                full_name="Owner",
                role=UserRole.TENANT_ADMIN,
                password_hash=hash_password(password),
            )
        )
    return email


async def _login(tenant_client, email: str, password: str) -> None:
    resp = await tenant_client.post(
        "/auth/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


async def test_logged_in_visitor_redirected_from_login(
    tenant_client, owner_engine, demo_tenant
) -> None:
    email = await _seed_admin(owner_engine, demo_tenant)
    await _login(tenant_client, email, "ownerpass-123")

    resp = await tenant_client.get("/auth/login", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app"


async def test_login_next_param_passes_through_safely(
    tenant_client, owner_engine, demo_tenant
) -> None:
    email = await _seed_admin(owner_engine, demo_tenant)
    await _login(tenant_client, email, "ownerpass-123")

    resp = await tenant_client.get("/auth/login?next=/app/orders", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app/orders"


async def test_login_next_param_open_redirect_rejected(
    tenant_client, owner_engine, demo_tenant
) -> None:
    """``next`` pointing off-site must not leak — fall back to /app."""
    email = await _seed_admin(owner_engine, demo_tenant)
    await _login(tenant_client, email, "ownerpass-123")

    resp = await tenant_client.get(
        "/auth/login?next=https://evil.example.com/steal", follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app"


async def test_logged_in_visitor_redirected_from_password_reset(
    tenant_client, owner_engine, demo_tenant
) -> None:
    """Signed-in users reset passwords via /app/admin/profile, not the
    public forgot-password flow."""
    email = await _seed_admin(owner_engine, demo_tenant)
    await _login(tenant_client, email, "ownerpass-123")

    resp = await tenant_client.get("/auth/password-reset", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app/admin/profile"


async def test_tenant_index_logged_in_skips_to_portal(
    tenant_client, owner_engine, demo_tenant
) -> None:
    email = await _seed_admin(owner_engine, demo_tenant)
    await _login(tenant_client, email, "ownerpass-123")

    resp = await tenant_client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app"


async def test_tenant_index_anonymous_renders_landing(tenant_client, demo_tenant) -> None:
    """Anon visitors still get the landing with its primary CTA."""
    resp = await tenant_client.get("/")
    assert resp.status_code == 200
    assert "Přihlásit se" in resp.text or "Sign in" in resp.text
