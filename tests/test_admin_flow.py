"""E2E tests for tenant admin flows: staff invite, password change, reset."""

from __future__ import annotations

import re
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.email.sender import CaptureSender
from app.models.enums import UserRole
from app.models.user import User
from app.security.passwords import hash_password, verify_password

pytestmark = pytest.mark.postgres


async def _seed_owner(owner_engine, tenant_id) -> None:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        session.add(
            User(
                id=uuid4(),
                tenant_id=tenant_id,
                email="owner@4mex.cz",
                full_name="4MEX Owner",
                role=UserRole.TENANT_ADMIN,
                password_hash=hash_password("ownerpass"),
            )
        )


async def _login(client: AsyncClient, email: str, password: str) -> None:
    resp = await client.post(
        "/auth/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


def _capture(client: AsyncClient) -> CaptureSender:
    capture = CaptureSender()
    client._transport.app.state.email_sender = capture  # type: ignore[attr-defined]
    return capture


# -------------------------------------------------------- staff invite


async def test_staff_invite_flow_happy_path(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed_owner(owner_engine, demo_tenant.id)
    capture = _capture(tenant_client)

    # Owner logs in and opens the team page.
    await _login(tenant_client, "owner@4mex.cz", "ownerpass")
    page = await tenant_client.get("/app/admin/users")
    assert page.status_code == 200
    assert "Uživatelé týmu" in page.text
    assert "4MEX Owner" in page.text

    # Invite a new staff member.
    resp = await tenant_client.post(
        "/app/admin/users/invite",
        data={
            "email": "vyroba@4mex.cz",
            "full_name": "Výroba",
            "role": "tenant_staff",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    # The row should exist, but with password_hash=None (pending accept).
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        invited = (
            await session.execute(select(User).where(User.email == "vyroba@4mex.cz"))
        ).scalar_one()
    assert invited.password_hash is None
    assert invited.is_active is True
    assert invited.role == UserRole.TENANT_STAFF

    # BackgroundTasks should have captured the invite email.
    assert len(capture.outbox) == 1
    msg = capture.outbox[0]
    assert msg.to == "vyroba@4mex.cz"
    assert "Pozvánka do týmu" in msg.subject

    match = re.search(r"/invite/staff\?token=([\w\-.]+)", msg.html)
    assert match, msg.html
    token = match.group(1)

    # Invitee opens the accept page. Use a fresh cookie jar.
    tenant_client.cookies.clear()
    accept_get = await tenant_client.get(f"/invite/staff?token={token}")
    assert accept_get.status_code == 200
    assert "Výroba" in accept_get.text

    # Posts the new password.
    accept_post = await tenant_client.post(
        "/invite/staff",
        data={
            "token": token,
            "password": "newstaffpass",
            "password_confirm": "newstaffpass",
        },
        follow_redirects=False,
    )
    assert accept_post.status_code == 303
    assert accept_post.headers["location"] == "/app"

    # The user should be active with a password hash.
    async with sm() as session:
        refreshed = (
            await session.execute(select(User).where(User.email == "vyroba@4mex.cz"))
        ).scalar_one()
    assert refreshed.password_hash is not None
    assert verify_password("newstaffpass", refreshed.password_hash)

    # Staff admin list now shows the new user as active.
    tenant_client.cookies.clear()
    await _login(tenant_client, "owner@4mex.cz", "ownerpass")
    list_page = await tenant_client.get("/app/admin/users")
    assert "Výroba" in list_page.text
    assert "aktivní" in list_page.text


async def test_duplicate_staff_invite_shows_error(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed_owner(owner_engine, demo_tenant.id)
    _capture(tenant_client)
    await _login(tenant_client, "owner@4mex.cz", "ownerpass")

    # Invite once.
    await tenant_client.post(
        "/app/admin/users/invite",
        data={"email": "dup@4mex.cz", "full_name": "Dup", "role": "tenant_staff"},
        follow_redirects=False,
    )
    # Second invite with the same email should 400.
    again = await tenant_client.post(
        "/app/admin/users/invite",
        data={"email": "dup@4mex.cz", "full_name": "Dup2", "role": "tenant_staff"},
        follow_redirects=False,
    )
    assert again.status_code == 400
    assert "už existuje" in again.text


async def test_non_admin_staff_cannot_invite(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    # Seed a plain staff user (NOT admin) directly in the DB.
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        session.add(
            User(
                id=uuid4(),
                tenant_id=demo_tenant.id,
                email="plain@4mex.cz",
                full_name="Plain",
                role=UserRole.TENANT_STAFF,
                password_hash=hash_password("plainpass"),
            )
        )

    await _login(tenant_client, "plain@4mex.cz", "plainpass")
    page = await tenant_client.get("/app/admin/users")
    assert page.status_code == 403


async def test_self_disable_forbidden(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed_owner(owner_engine, demo_tenant.id)
    await _login(tenant_client, "owner@4mex.cz", "ownerpass")

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        owner = (
            await session.execute(select(User).where(User.email == "owner@4mex.cz"))
        ).scalar_one()

    resp = await tenant_client.post(f"/app/admin/users/{owner.id}/disable", follow_redirects=False)
    assert resp.status_code == 400


# ---------------------------------------------------- password change


async def test_password_change_bumps_session_version_and_forces_relogin(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed_owner(owner_engine, demo_tenant.id)
    await _login(tenant_client, "owner@4mex.cz", "ownerpass")

    resp = await tenant_client.post(
        "/app/admin/profile/password",
        data={
            "current_password": "ownerpass",
            "new_password": "brandnewpass",
            "new_password_confirm": "brandnewpass",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"

    # Old cookie is now invalid because session_version bumped.
    page = await tenant_client.get(
        "/app/admin/users",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert page.status_code == 303
    assert page.headers["location"].startswith("/auth/login")

    # Log in with new password works.
    tenant_client.cookies.clear()
    await _login(tenant_client, "owner@4mex.cz", "brandnewpass")


async def test_password_change_rejects_wrong_current(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed_owner(owner_engine, demo_tenant.id)
    await _login(tenant_client, "owner@4mex.cz", "ownerpass")

    resp = await tenant_client.post(
        "/app/admin/profile/password",
        data={
            "current_password": "WRONG",
            "new_password": "brandnewpass",
            "new_password_confirm": "brandnewpass",
        },
    )
    assert resp.status_code == 400
    assert "current password is incorrect" in resp.text


# ------------------------------------------------------ password reset


async def test_password_reset_full_loop(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed_owner(owner_engine, demo_tenant.id)
    capture = _capture(tenant_client)

    # Request a reset email.
    req = await tenant_client.post(
        "/auth/password-reset",
        data={"email": "owner@4mex.cz"},
    )
    assert req.status_code == 200
    assert "Pokud adresa existuje" in req.text

    assert len(capture.outbox) == 1
    msg = capture.outbox[0]
    assert msg.to == "owner@4mex.cz"
    assert "Obnovení hesla" in msg.subject

    match = re.search(r"/auth/password-reset/confirm\?token=([\w\-.]+)", msg.html)
    assert match, msg.html
    token = match.group(1)

    # Follow the reset link and submit a new password.
    confirm_resp = await tenant_client.post(
        "/auth/password-reset/confirm",
        data={
            "token": token,
            "password": "fresh-pass",
            "password_confirm": "fresh-pass",
        },
        follow_redirects=False,
    )
    assert confirm_resp.status_code == 303
    assert "/auth/login" in confirm_resp.headers["location"]

    # Old password must no longer work.
    bad = await tenant_client.post(
        "/auth/login",
        data={"email": "owner@4mex.cz", "password": "ownerpass"},
    )
    assert bad.status_code == 401

    # New password logs in.
    await _login(tenant_client, "owner@4mex.cz", "fresh-pass")


async def test_password_reset_for_unknown_email_does_not_leak(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed_owner(owner_engine, demo_tenant.id)
    capture = _capture(tenant_client)

    resp = await tenant_client.post(
        "/auth/password-reset",
        data={"email": "nobody@nowhere.cz"},
    )
    assert resp.status_code == 200
    # Same notice — no enumeration.
    assert "Pokud adresa existuje" in resp.text
    # No email was sent.
    assert capture.outbox == []
