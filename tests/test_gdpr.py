"""E2E tests for the GDPR staff-profile routes (F-BE-001 / F-SEC-001).

Covers /app/admin/profile/export (Art. 20 portability) and
/app/admin/profile/delete (Art. 17 erasure): the password gate, the
last-admin lockout, the anonymisation itself, the audit row, and the
forced logout after erasure.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.audit_event import AuditEvent
from app.models.enums import UserRole
from app.models.user import User
from app.services.gdpr_service import ANONYMIZED_LABEL

pytestmark = pytest.mark.postgres


async def _seed_admin(owner_engine, tenant_id, *, email: str, password_hash: str) -> User:
    from app.security.passwords import hash_password

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        user = User(
            id=uuid4(),
            tenant_id=tenant_id,
            email=email,
            full_name="4MEX Owner" if email.startswith("owner") else "Second Admin",
            role=UserRole.TENANT_ADMIN,
            password_hash=hash_password(password_hash),
        )
        session.add(user)
    return user


async def _login(client: AsyncClient, email: str, password: str) -> None:
    resp = await client.post(
        "/auth/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


async def _user_row(owner_engine, email_or_id) -> User:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        if isinstance(email_or_id, str):
            q = select(User).where(User.email == email_or_id)
        else:
            q = select(User).where(User.id == email_or_id)
        return (await session.execute(q)).scalar_one()


# ------------------------------------------------------------- export


async def test_profile_export_returns_json_attachment(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed_admin(
        owner_engine, demo_tenant.id, email="owner@4mex.cz", password_hash="ownerpass"
    )
    await _login(tenant_client, "owner@4mex.cz", "ownerpass")

    resp = await tenant_client.get("/app/admin/profile/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.headers["content-disposition"].startswith("attachment;")
    assert "owner@4mex.cz" in resp.headers["content-disposition"]

    payload = resp.json()
    assert payload["kind"] == "user"
    assert payload["profile"]["email"] == "owner@4mex.cz"
    assert payload["profile"]["full_name"] == "4MEX Owner"
    assert payload["profile"]["role"] == "tenant_admin"
    # Portability shape: the collection keys must exist even when empty.
    assert payload["orders_created"] == []
    assert isinstance(payload["audit_events_authored"], list)


# ------------------------------------------------------------- delete


async def test_profile_delete_wrong_password_cancels(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    seeded = await _seed_admin(
        owner_engine, demo_tenant.id, email="owner@4mex.cz", password_hash="ownerpass"
    )
    await _login(tenant_client, "owner@4mex.cz", "ownerpass")

    resp = await tenant_client.post(
        "/app/admin/profile/delete",
        data={"password": "not-the-password"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/app/admin/profile?error=")

    # Nothing was mutated: same email, still active, session still valid.
    row = await _user_row(owner_engine, seeded.id)
    assert row.email == "owner@4mex.cz"
    assert row.is_active is True
    assert row.password_hash is not None
    page = await tenant_client.get("/app/admin/profile", follow_redirects=False)
    assert page.status_code == 200


async def test_profile_delete_blocks_last_admin(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    seeded = await _seed_admin(
        owner_engine, demo_tenant.id, email="owner@4mex.cz", password_hash="ownerpass"
    )
    await _login(tenant_client, "owner@4mex.cz", "ownerpass")

    resp = await tenant_client.post(
        "/app/admin/profile/delete",
        data={"password": "ownerpass"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/app/admin/profile?error=")

    row = await _user_row(owner_engine, seeded.id)
    assert row.email == "owner@4mex.cz"
    assert row.is_active is True


async def test_profile_delete_happy_path_anonymises_and_logs_out(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    seeded = await _seed_admin(
        owner_engine, demo_tenant.id, email="owner@4mex.cz", password_hash="ownerpass"
    )
    # A second active admin so the last-admin guard doesn't trip.
    await _seed_admin(
        owner_engine, demo_tenant.id, email="second@4mex.cz", password_hash="secondpass"
    )
    await _login(tenant_client, "owner@4mex.cz", "ownerpass")
    version_before = (await _user_row(owner_engine, seeded.id)).session_version

    resp = await tenant_client.post(
        "/app/admin/profile/delete",
        data={"password": "ownerpass"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login?notice=account_deleted"

    # Row retained but PII anonymised; login blocked; sessions invalidated.
    row = await _user_row(owner_engine, seeded.id)
    assert row.email == f"erased-user-{seeded.id}@erased.invalid"
    assert row.full_name == ANONYMIZED_LABEL
    assert row.password_hash is None
    assert row.preferred_locale is None
    assert row.is_active is False
    assert row.session_version == version_before + 1
    assert "_gdpr_erased_at" in (row.notification_prefs or {})

    # Audit trail keeps the original label for the operator's timeline.
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.action == "user.gdpr_erased",
                        AuditEvent.entity_id == seeded.id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    assert events[0].entity_label == "4MEX Owner <owner@4mex.cz>"

    # The old session cookie must be dead now.
    page = await tenant_client.get("/app/admin/profile", follow_redirects=False)
    assert page.status_code in (302, 303, 401)

    # And the erased credentials can no longer log in.
    relogin = await tenant_client.post(
        "/auth/login",
        data={"email": "owner@4mex.cz", "password": "ownerpass"},
        follow_redirects=False,
    )
    assert relogin.status_code != 303


async def test_profile_delete_allows_non_last_admin_staff(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """A plain STAFF user is never the 'last admin' — self-erasure works
    even when they are the only staff row, as long as an admin exists."""
    from app.security.passwords import hash_password

    await _seed_admin(
        owner_engine, demo_tenant.id, email="owner@4mex.cz", password_hash="ownerpass"
    )
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    staff_id = uuid4()
    async with sm() as session, session.begin():
        session.add(
            User(
                id=staff_id,
                tenant_id=demo_tenant.id,
                email="plain@4mex.cz",
                full_name="Plain Staff",
                role=UserRole.TENANT_STAFF,
                password_hash=hash_password("plainpass"),
            )
        )

    await _login(tenant_client, "plain@4mex.cz", "plainpass")
    resp = await tenant_client.post(
        "/app/admin/profile/delete",
        data={"password": "plainpass"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login?notice=account_deleted"

    row = await _user_row(owner_engine, staff_id)
    assert row.full_name == ANONYMIZED_LABEL
    assert row.is_active is False
