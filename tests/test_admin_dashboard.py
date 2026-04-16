"""Platform admin dashboard — KPI cards + recent signups."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.main import create_app
from app.platform.models import Identity
from app.security.passwords import hash_password
from tests.conftest import CsrfAwareClient

pytestmark = pytest.mark.postgres


@pytest.fixture
async def admin_client(settings, wipe_db, owner_engine) -> AsyncIterator[CsrfAwareClient]:
    settings.feature_platform = True

    async with owner_engine.begin() as conn:
        await conn.execute(text("DELETE FROM platform_tenant_memberships"))
        await conn.execute(text("DELETE FROM platform_identities"))
        await conn.execute(text("DELETE FROM platform_subscriptions"))
        await conn.execute(text("DELETE FROM platform_invoices"))

    # Seed a platform admin.
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        admin = Identity(
            id=uuid4(),
            email="root@platform.local",
            full_name="Root",
            password_hash=hash_password("rootpass"),
            is_platform_admin=True,
        )
        session.add(admin)

    from app.platform.deps import reset_platform_engine

    reset_platform_engine()

    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with CsrfAwareClient(transport=transport, base_url="http://testserver") as ac:
        yield ac

    reset_platform_engine()


async def _login_admin(client: CsrfAwareClient) -> None:
    resp = await client.post(
        "/platform/login",
        data={"email": "root@platform.local", "password": "rootpass"},
        follow_redirects=False,
    )
    assert resp.status_code == 303


async def test_admin_dashboard_requires_platform_admin(admin_client) -> None:
    # No login → 401 (from require_platform_admin via require_identity).
    resp = await admin_client.get("/platform/admin/dashboard", follow_redirects=False)
    assert resp.status_code in (303, 401)


async def test_admin_dashboard_renders_empty(admin_client) -> None:
    await _login_admin(admin_client)
    resp = await admin_client.get("/platform/admin/dashboard")
    assert resp.status_code == 200
    assert "Platform Admin" in resp.text
    assert "Celkem tenantů" in resp.text
    assert "MRR" in resp.text


async def test_admin_dashboard_reflects_recent_signups(admin_client) -> None:
    await _login_admin(admin_client)

    # Sign up a couple of tenants via the public signup form.
    for i in range(3):
        resp = await admin_client.post(
            "/platform/signup",
            data={
                "company_name": f"ACME-{i}",
                "slug": f"acme-{i}",
                "owner_email": f"owner{i}@acme-{i}.cz",
                "owner_full_name": f"Owner {i}",
                "password": "correct-horse-battery-staple",
                "terms_accepted": "1",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        # Signup logs us in — clear platform cookie so next signup works.
        admin_client.cookies.pop("sme_portal_platform", None)

    # Re-login as admin.
    await _login_admin(admin_client)
    resp = await admin_client.get("/platform/admin/dashboard")
    assert resp.status_code == 200
    for i in range(3):
        assert f"acme-{i}" in resp.text
    # 3 tenants created + the signups counter should show 3.
    assert "aktivních" in resp.text
