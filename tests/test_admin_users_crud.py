"""Full CRUD lifecycle for team users.

Previously /app/admin/users supported only invite + disable; a
disabled user had no reactivate action, no edit, no resend-invite.
The Sprint-A commit added those routes — regression coverage here.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.enums import UserRole
from app.models.user import User
from app.security.passwords import hash_password

pytestmark = pytest.mark.postgres


async def _seed_admin(owner_engine, demo_tenant, *, password: str = "admin-123-pwd") -> str:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    email = f"alice-{uuid4().hex[:6]}@4mex.cz"
    async with sm() as session, session.begin():
        session.add(
            User(
                tenant_id=demo_tenant.id,
                email=email,
                full_name="Alice Admin",
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


async def _invite(tenant_client, *, email: str, full_name: str, role: str = "tenant_staff") -> None:
    resp = await tenant_client.post(
        "/app/admin/users/invite",
        data={"email": email, "full_name": full_name, "role": role},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


async def test_reactivate_restores_is_active(tenant_client, owner_engine, demo_tenant) -> None:
    admin_email = await _seed_admin(owner_engine, demo_tenant)
    await _login(tenant_client, admin_email, "admin-123-pwd")
    await _invite(tenant_client, email="bob@4mex.cz", full_name="Bob Member")

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        bob = (await session.execute(select(User).where(User.email == "bob@4mex.cz"))).scalar_one()

    # Disable bob, then reactivate.
    resp = await tenant_client.post(f"/app/admin/users/{bob.id}/disable", follow_redirects=False)
    assert resp.status_code == 303

    resp = await tenant_client.post(f"/app/admin/users/{bob.id}/reactivate", follow_redirects=False)
    assert resp.status_code == 303
    assert "notice=" in resp.headers["location"]

    async with sm() as session:
        bob_fresh = (await session.execute(select(User).where(User.id == bob.id))).scalar_one()
        assert bob_fresh.is_active is True


async def test_edit_changes_name_and_role(tenant_client, owner_engine, demo_tenant) -> None:
    admin_email = await _seed_admin(owner_engine, demo_tenant)
    await _login(tenant_client, admin_email, "admin-123-pwd")
    await _invite(tenant_client, email="carol@4mex.cz", full_name="Old Name")

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        carol = (
            await session.execute(select(User).where(User.email == "carol@4mex.cz"))
        ).scalar_one()
        assert carol.role == UserRole.TENANT_STAFF

    resp = await tenant_client.post(
        f"/app/admin/users/{carol.id}/edit",
        data={"full_name": "New Name", "role": "tenant_admin"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    async with sm() as session:
        carol_fresh = (await session.execute(select(User).where(User.id == carol.id))).scalar_one()
        assert carol_fresh.full_name == "New Name"
        assert carol_fresh.role == UserRole.TENANT_ADMIN


async def test_edit_rejects_empty_name(tenant_client, owner_engine, demo_tenant) -> None:
    admin_email = await _seed_admin(owner_engine, demo_tenant)
    await _login(tenant_client, admin_email, "admin-123-pwd")
    await _invite(tenant_client, email="dave@4mex.cz", full_name="Dave")

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        dave = (
            await session.execute(select(User).where(User.email == "dave@4mex.cz"))
        ).scalar_one()

    resp = await tenant_client.post(
        f"/app/admin/users/{dave.id}/edit",
        data={"full_name": "   ", "role": "tenant_staff"},
    )
    assert resp.status_code == 400
    # Dave's name unchanged.
    async with sm() as session:
        dave_fresh = (await session.execute(select(User).where(User.id == dave.id))).scalar_one()
        assert dave_fresh.full_name == "Dave"


async def test_resend_invite_blocks_when_password_set(
    tenant_client, owner_engine, demo_tenant
) -> None:
    """Resend-invite is only valid while the user hasn't accepted the
    original invitation. Once a password is set, forcing a new token
    would be confusing — point to the password-reset flow instead."""
    admin_email = await _seed_admin(owner_engine, demo_tenant)
    await _login(tenant_client, admin_email, "admin-123-pwd")

    # Seed a user who already has a password.
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    eve_id = uuid4()
    async with sm() as session, session.begin():
        session.add(
            User(
                id=eve_id,
                tenant_id=demo_tenant.id,
                email="eve@4mex.cz",
                full_name="Eve",
                role=UserRole.TENANT_STAFF,
                password_hash=hash_password("already-set-123"),
            )
        )

    resp = await tenant_client.post(
        f"/app/admin/users/{eve_id}/resend-invite", follow_redirects=False
    )
    assert resp.status_code == 303
    # Redirect carries an ?error= flash.
    assert "error=" in resp.headers["location"]


async def test_self_actions_disallowed(tenant_client, owner_engine, demo_tenant) -> None:
    """A user cannot disable themselves (would brick the tenant), and
    cannot demote themselves out of tenant_admin."""
    admin_email = await _seed_admin(owner_engine, demo_tenant)
    await _login(tenant_client, admin_email, "admin-123-pwd")

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        me = (await session.execute(select(User).where(User.email == admin_email))).scalar_one()

    resp = await tenant_client.post(f"/app/admin/users/{me.id}/disable", follow_redirects=False)
    assert resp.status_code == 400

    resp = await tenant_client.post(
        f"/app/admin/users/{me.id}/edit",
        data={"full_name": "New Me", "role": "tenant_staff"},
    )
    assert resp.status_code == 400
