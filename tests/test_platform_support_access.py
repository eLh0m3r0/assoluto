"""Platform admin support-access grant + revoke round-trip.

Covers the ``access_type`` column on ``TenantMembership``, the grant /
revoke service helpers, and the template surfaces that show the badge
and the revoke button:

* ``grant_platform_admin_support_access`` marks the new membership with
  ``access_type="support"``; repeated calls stay idempotent.
* ``revoke_platform_admin_support_access`` drops the membership and
  deactivates the created User row without hard-deleting it (audit
  trail integrity).
* ``/platform/select-tenant`` renders a ⚙ Support badge for the
  support membership.
* ``/platform/admin/tenants`` shows the caller's access state in the
  "Váš přístup" column (member / support / none).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.main import create_app
from app.models.tenant import Tenant
from app.models.user import User
from app.platform.models import (
    MEMBERSHIP_ACCESS_MEMBER,
    MEMBERSHIP_ACCESS_SUPPORT,
    Identity,
    TenantMembership,
)
from app.platform.service import (
    grant_platform_admin_support_access,
    revoke_platform_admin_support_access,
)
from tests.conftest import CsrfAwareClient

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------- fixtures


@pytest.fixture
async def platform_client(settings, wipe_db, owner_engine) -> AsyncIterator[CsrfAwareClient]:
    settings.feature_platform = True
    settings.stripe_secret_key = ""
    settings.platform_cookie_domain = ""

    async with owner_engine.begin() as conn:
        await conn.execute(text("DELETE FROM platform_tenant_memberships"))
        await conn.execute(text("DELETE FROM platform_identities"))
        await conn.execute(text("DELETE FROM platform_subscriptions"))
        await conn.execute(text("DELETE FROM platform_invoices"))

    from app.platform.deps import reset_platform_engine

    reset_platform_engine()

    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with CsrfAwareClient(transport=transport, base_url="http://testserver") as ac:
        yield ac

    reset_platform_engine()


async def _seed_admin_and_tenant(
    owner_engine,
    *,
    admin_email: str = "admin@platform.cz",
    admin_password: str = "AdminPwd-123",
    tenant_slug: str = "acme",
    tenant_name: str = "ACME s.r.o.",
) -> tuple[Identity, Tenant]:
    from app.models.enums import UserRole
    from app.platform.service import create_or_get_identity
    from app.security.passwords import hash_password

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        identity = await create_or_get_identity(
            session,
            email=admin_email,
            full_name="Platform Admin",
            password=admin_password,
            pre_verified=True,
        )
        identity.is_platform_admin = True
        identity.password_hash = hash_password(admin_password)

        tenant = Tenant(
            slug=tenant_slug,
            name=tenant_name,
            billing_email=f"billing@{tenant_slug}.cz",
            storage_prefix=f"tenants/{tenant_slug}/",
        )
        session.add(tenant)
        await session.flush()

        # Seed one normal user so the tenant isn't empty — exercising
        # the unique constraint on (tenant, email) separately from the
        # support flow.
        session.add(
            User(
                tenant_id=tenant.id,
                email="owner@acme.cz",
                full_name="Tenant Owner",
                role=UserRole.TENANT_ADMIN,
                password_hash=hash_password("x" * 12),
            )
        )
        await session.flush()

    return identity, tenant


async def _platform_login(client: CsrfAwareClient, email: str, password: str) -> None:
    resp = await client.post(
        "/platform/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


# ---------------------------------------------------------------- service-level


async def test_grant_support_access_marks_membership_as_support(
    platform_client, owner_engine
) -> None:
    identity, tenant = await _seed_admin_and_tenant(owner_engine)

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        fresh_identity = (
            await session.execute(select(Identity).where(Identity.id == identity.id))
        ).scalar_one()
        user, membership = await grant_platform_admin_support_access(
            session, identity=fresh_identity, tenant_id=tenant.id
        )
        assert membership.access_type == MEMBERSHIP_ACCESS_SUPPORT
        assert user.tenant_id == tenant.id
        assert user.email == identity.email.lower()
        assert user.password_hash is None  # login via platform handoff only
        assert user.is_active is True


async def test_grant_support_access_is_idempotent(platform_client, owner_engine) -> None:
    identity, tenant = await _seed_admin_and_tenant(owner_engine)

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        fresh_identity = (
            await session.execute(select(Identity).where(Identity.id == identity.id))
        ).scalar_one()
        user1, m1 = await grant_platform_admin_support_access(
            session, identity=fresh_identity, tenant_id=tenant.id
        )
        user2, m2 = await grant_platform_admin_support_access(
            session, identity=fresh_identity, tenant_id=tenant.id
        )
        assert m1.id == m2.id
        assert user1.id == user2.id

    # Still a single membership row.
    async with sm() as session:
        count = (
            (
                await session.execute(
                    select(TenantMembership).where(
                        TenantMembership.identity_id == identity.id,
                        TenantMembership.tenant_id == tenant.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(count) == 1
        assert count[0].access_type == MEMBERSHIP_ACCESS_SUPPORT


async def test_revoke_support_access_removes_membership_and_deactivates_user(
    platform_client, owner_engine
) -> None:
    identity, tenant = await _seed_admin_and_tenant(owner_engine)

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)

    # Grant first.
    async with sm() as session, session.begin():
        fresh_identity = (
            await session.execute(select(Identity).where(Identity.id == identity.id))
        ).scalar_one()
        await grant_platform_admin_support_access(
            session, identity=fresh_identity, tenant_id=tenant.id
        )

    # Revoke.
    async with sm() as session, session.begin():
        fresh_identity = (
            await session.execute(select(Identity).where(Identity.id == identity.id))
        ).scalar_one()
        result = await revoke_platform_admin_support_access(
            session, identity=fresh_identity, tenant_id=tenant.id
        )
        assert result is not None
        user, _ = result
        assert user is not None
        assert user.is_active is False  # disabled, not deleted

    # Membership row gone, user row remains (for FK integrity).
    async with sm() as session:
        membership = (
            await session.execute(
                select(TenantMembership).where(
                    TenantMembership.identity_id == identity.id,
                    TenantMembership.tenant_id == tenant.id,
                )
            )
        ).scalar_one_or_none()
        assert membership is None

        user_row = (
            await session.execute(
                select(User).where(User.tenant_id == tenant.id, User.email == identity.email)
            )
        ).scalar_one()
        assert user_row.is_active is False


async def test_revoke_support_is_idempotent_when_nothing_to_revoke(
    platform_client, owner_engine
) -> None:
    identity, tenant = await _seed_admin_and_tenant(owner_engine)

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        fresh_identity = (
            await session.execute(select(Identity).where(Identity.id == identity.id))
        ).scalar_one()
        # No grant yet — revoke must be a no-op (not raise).
        result = await revoke_platform_admin_support_access(
            session, identity=fresh_identity, tenant_id=tenant.id
        )
        assert result is None


# ---------------------------------------------------------------- default


async def test_default_access_type_is_member(platform_client, owner_engine) -> None:
    """Regular tenant-membership rows (e.g. from signup / invite) must
    default to 'member' so the admin view and select-tenant badges
    render correctly for anyone who existed before the column landed."""
    _identity, tenant = await _seed_admin_and_tenant(owner_engine)

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        other_identity = Identity(
            email="bob@acme.cz",
            full_name="Bob Member",
            password_hash="x" * 60,
            email_verified_at=None,
        )
        session.add(other_identity)
        await session.flush()

        # Link to the existing owner user via the normal helper —
        # purely for testing, we fake it with a direct membership row.
        user = (
            await session.execute(
                select(User).where(User.tenant_id == tenant.id, User.email == "owner@acme.cz")
            )
        ).scalar_one()
        membership = TenantMembership(
            identity_id=other_identity.id,
            tenant_id=tenant.id,
            user_id=user.id,
            contact_id=None,
        )
        session.add(membership)
        await session.flush()

        # Server-default kicks in.
        assert membership.access_type == MEMBERSHIP_ACCESS_MEMBER


# ---------------------------------------------------------------- HTTP surfaces


async def test_admin_tenants_shows_access_state(platform_client, owner_engine) -> None:
    identity, tenant = await _seed_admin_and_tenant(owner_engine)
    await _platform_login(platform_client, identity.email, "AdminPwd-123")

    # Before any grant — column reads "—" (dash).
    resp = await platform_client.get("/platform/admin/tenants")
    assert resp.status_code == 200
    assert "Váš přístup" in resp.text

    # Grant via the route and re-fetch — badge updates to 'support'
    # and a "Zrušit support" action appears.
    resp = await platform_client.post(
        f"/platform/admin/tenants/{tenant.id}/support-access",
        follow_redirects=False,
    )
    assert resp.status_code == 303

    resp = await platform_client.get("/platform/admin/tenants")
    assert resp.status_code == 200
    assert "⚙ support" in resp.text
    assert "Zrušit support" in resp.text

    # Revoke — badge disappears, grant button returns.
    resp = await platform_client.post(
        f"/platform/admin/tenants/{tenant.id}/revoke-support",
        follow_redirects=False,
    )
    assert resp.status_code == 303

    resp = await platform_client.get("/platform/admin/tenants")
    assert resp.status_code == 200
    assert "Zrušit support" not in resp.text
    assert "Support přístup" in resp.text  # grant CTA back


async def test_select_tenant_renders_support_badge(platform_client, owner_engine) -> None:
    identity, tenant = await _seed_admin_and_tenant(owner_engine)
    await _platform_login(platform_client, identity.email, "AdminPwd-123")

    # Grant support → membership shows up on select-tenant.
    resp = await platform_client.post(
        f"/platform/admin/tenants/{tenant.id}/support-access",
        follow_redirects=False,
    )
    assert resp.status_code == 303

    resp = await platform_client.get("/platform/select-tenant")
    assert resp.status_code == 200
    # Badge copy from the template.
    assert "Support" in resp.text or "support" in resp.text
    # The tenant appears at least once.
    assert tenant.slug in resp.text
