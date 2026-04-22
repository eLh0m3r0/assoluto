"""Session-fixation regression: a pre-existing attacker-chosen session
cookie must be REPLACED on successful login, not extended.

The itsdangerous serializer embeds a timestamp in every emitted value,
so two signatures over the same payload still differ. But the stronger
guarantee we rely on is that ``response.set_cookie(SESSION_COOKIE_NAME,
...)`` fully replaces any inbound cookie of the same name — the
browser discards the old one the next request.

The test exercises login via the real route to make sure nothing in
the stack accidentally keeps or echoes the attacker-supplied cookie.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.enums import UserRole
from app.models.user import User
from app.security.passwords import hash_password
from app.security.session import SESSION_COOKIE_NAME, SessionData, _serializer

pytestmark = pytest.mark.postgres


async def test_login_replaces_prior_session_cookie(
    tenant_client, owner_engine, demo_tenant
) -> None:
    # Seed a real user.
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        user = User(
            id=uuid4(),
            tenant_id=demo_tenant.id,
            email=f"fix-{uuid4().hex[:6]}@4mex.cz",
            full_name="Fix Test",
            role=UserRole.TENANT_ADMIN,
            password_hash=hash_password("correct-horse-battery"),
        )
        session.add(user)
        await session.flush()

    # Attacker-planted cookie.
    planted = _serializer("unit-test-secret").dumps(
        SessionData(
            principal_type="user",
            principal_id=str(uuid4()),
            tenant_id=str(demo_tenant.id),
        ).to_dict()
    )
    tenant_client.cookies.set(SESSION_COOKIE_NAME, planted)

    resp = await tenant_client.post(
        "/auth/login",
        data={"email": user.email, "password": "correct-horse-battery"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    # Either a Set-Cookie with a different value OR no cookie at all
    # (latter would fail login, so we expect the former). Extract the
    # new value from the response Set-Cookie header.
    set_cookie = [
        h for n, h in resp.headers.items() if n.lower() == "set-cookie" and SESSION_COOKIE_NAME in h
    ]
    assert set_cookie, "login must emit a fresh Set-Cookie for the session"
    new_value = set_cookie[0].split("=", 1)[1].split(";", 1)[0]
    assert new_value != planted, "attacker-planted cookie must not be kept"
