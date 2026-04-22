"""Platform layer (SaaS) E2E tests.

The platform package is opt-in via FEATURE_PLATFORM; these tests
flip the flag on for the duration of each test via a dedicated
`platform_client` fixture that builds a fresh app with the feature
enabled.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import ASGITransport
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.main import create_app
from app.models.tenant import Tenant
from app.platform.models import Identity, TenantMembership
from app.security.passwords import hash_password
from tests.conftest import CsrfAwareClient

pytestmark = pytest.mark.postgres


@pytest.fixture
async def platform_settings(settings):  # type: ignore[misc]
    """Settings with FEATURE_PLATFORM=True."""
    settings.feature_platform = True
    return settings


@pytest.fixture
async def platform_client(
    platform_settings, wipe_db, owner_engine
) -> AsyncIterator[CsrfAwareClient]:
    # Also wipe platform tables.
    async with owner_engine.begin() as conn:
        await conn.execute(text("DELETE FROM platform_tenant_memberships"))
        await conn.execute(text("DELETE FROM platform_identities"))

    # Reset the platform package's engine cache so it picks up the
    # session-scoped Postgres fixtures in a clean state.
    from app.platform.deps import reset_platform_engine

    reset_platform_engine()

    app = create_app(platform_settings)
    transport = ASGITransport(app=app)
    async with CsrfAwareClient(transport=transport, base_url="http://testserver") as ac:
        yield ac

    reset_platform_engine()


async def _seed_platform_admin(owner_engine) -> dict:
    from datetime import UTC, datetime

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        admin = Identity(
            id=uuid4(),
            email="root@platform.local",
            full_name="Platform Root",
            password_hash=hash_password("rootpass"),
            is_platform_admin=True,
            # require_platform_admin now gates on email_verified_at too.
            email_verified_at=datetime.now(UTC),
        )
        session.add(admin)
        await session.flush()
    return {"admin_id": admin.id}


async def _platform_login(client: CsrfAwareClient, email: str, password: str) -> None:
    resp = await client.post(
        "/platform/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


async def test_platform_login_form_renders(platform_client: CsrfAwareClient) -> None:
    resp = await platform_client.get("/platform/login")
    assert resp.status_code == 200
    # cs default locale: "Platform" → "Platforma".
    assert "Assoluto — Platforma" in resp.text


async def test_platform_login_with_wrong_password_fails(
    platform_client: CsrfAwareClient, owner_engine
) -> None:
    await _seed_platform_admin(owner_engine)

    resp = await platform_client.post(
        "/platform/login",
        data={"email": "root@platform.local", "password": "nope"},
    )
    assert resp.status_code == 401
    assert "Neplatný" in resp.text


async def test_platform_admin_creates_tenant_and_owner(
    platform_client: CsrfAwareClient, owner_engine
) -> None:
    await _seed_platform_admin(owner_engine)

    await _platform_login(platform_client, "root@platform.local", "rootpass")

    # Platform admin navigates to tenant list (empty).
    list_resp = await platform_client.get("/platform/admin/tenants")
    assert list_resp.status_code == 200
    assert "Zatím žádní tenanti" in list_resp.text

    # Create a new tenant.
    create_resp = await platform_client.post(
        "/platform/admin/tenants",
        data={
            "slug": "4mex",
            "name": "4MEX s.r.o.",
            "owner_email": "owner@4mex.cz",
            "owner_full_name": "4MEX Owner",
            "owner_password": "demo1234",
        },
        follow_redirects=False,
    )
    assert create_resp.status_code == 303

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        tenant = (await session.execute(select(Tenant).where(Tenant.slug == "4mex"))).scalar_one()
        identity = (
            await session.execute(select(Identity).where(Identity.email == "owner@4mex.cz"))
        ).scalar_one()
        membership = (
            await session.execute(
                select(TenantMembership).where(
                    TenantMembership.identity_id == identity.id,
                    TenantMembership.tenant_id == tenant.id,
                )
            )
        ).scalar_one()

    assert tenant.name == "4MEX s.r.o."
    assert tenant.is_active is True
    assert identity.full_name == "4MEX Owner"
    assert identity.is_platform_admin is False
    assert membership.user_id is not None


async def test_platform_owner_sees_tenant_in_select(
    platform_client: CsrfAwareClient, owner_engine
) -> None:
    await _seed_platform_admin(owner_engine)
    await _platform_login(platform_client, "root@platform.local", "rootpass")

    # Create a tenant.
    await platform_client.post(
        "/platform/admin/tenants",
        data={
            "slug": "4mex",
            "name": "4MEX s.r.o.",
            "owner_email": "owner@4mex.cz",
            "owner_full_name": "4MEX Owner",
            "owner_password": "demo1234",
        },
        follow_redirects=False,
    )

    # Log out, log back in as the new owner.
    await platform_client.post("/platform/logout", follow_redirects=False)
    platform_client.cookies.clear()
    await _platform_login(platform_client, "owner@4mex.cz", "demo1234")

    select_resp = await platform_client.get("/platform/select-tenant")
    assert select_resp.status_code == 200
    assert "4MEX s.r.o." in select_resp.text
    # Earlier copy was "Jako člen týmu"; the tenant-picker now uses a
    # compact "Člen týmu" badge (via the access_type label) instead of
    # a leading preposition. Accept both.
    assert "Člen týmu" in select_resp.text or "Jako člen týmu" in select_resp.text


async def test_non_admin_cannot_see_tenants_index(
    platform_client: CsrfAwareClient, owner_engine
) -> None:
    """Regular identities must hit 403 on the platform admin pages."""
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        session.add(
            Identity(
                id=uuid4(),
                email="joe@example.com",
                full_name="Joe",
                password_hash=hash_password("joepass"),
                is_platform_admin=False,
            )
        )

    await _platform_login(platform_client, "joe@example.com", "joepass")
    resp = await platform_client.get("/platform/admin/tenants")
    assert resp.status_code == 403


async def test_tenants_deactivate(platform_client: CsrfAwareClient, owner_engine) -> None:
    await _seed_platform_admin(owner_engine)
    await _platform_login(platform_client, "root@platform.local", "rootpass")

    await platform_client.post(
        "/platform/admin/tenants",
        data={
            "slug": "short-lived",
            "name": "Short-Lived",
            "owner_email": "who@cares.cz",
            "owner_full_name": "Who",
            "owner_password": "demo1234",
        },
        follow_redirects=False,
    )

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.slug == "short-lived"))
        ).scalar_one()

    resp = await platform_client.post(
        f"/platform/admin/tenants/{tenant.id}/deactivate",
        follow_redirects=False,
    )
    assert resp.status_code == 303

    async with sm() as session:
        refreshed = (
            await session.execute(select(Tenant).where(Tenant.id == tenant.id))
        ).scalar_one()
    assert refreshed.is_active is False


async def test_core_routes_unaffected_when_platform_on(
    platform_client: CsrfAwareClient, owner_engine, demo_tenant
) -> None:
    """Verify turning FEATURE_PLATFORM on doesn't break tenant-local auth."""
    # Seed a staff user the classic (non-platform) way.
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        from app.models.enums import UserRole as _UserRole
        from app.models.user import User as _User

        session.add(
            _User(
                id=uuid4(),
                tenant_id=demo_tenant.id,
                email="owner@4mex.cz",
                full_name="Owner",
                role=_UserRole.TENANT_ADMIN,
                password_hash=hash_password("staffpass"),
            )
        )

    # Tenant-local login still works via the X-Tenant-Slug header.
    resp = await platform_client.post(
        "/auth/login",
        data={"email": "owner@4mex.cz", "password": "staffpass"},
        headers={"X-Tenant-Slug": "4mex"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app"


async def test_platform_routes_not_mounted_when_flag_off(
    tenant_client: CsrfAwareClient,
) -> None:
    """Default (core) build: /platform/login returns 404, not 200."""
    resp = await tenant_client.get("/platform/login")
    assert resp.status_code == 404
