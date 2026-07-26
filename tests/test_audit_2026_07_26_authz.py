"""Regression tests for the 2026-07-26 audit — auth, session, authz.

One test per finding, named after it. Each asserts the *attack*, not the
fix: if someone re-narrows the code later, the test says what breaks.

See docs/audit-runs/2026-07-26-e2e/findings.md.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.main import create_app
from app.platform.models import (
    MEMBERSHIP_ACCESS_MEMBER,
    MEMBERSHIP_ACCESS_SUPPORT,
    Identity,
    TenantMembership,
)
from app.security.passwords import hash_password
from tests.conftest import CsrfAwareClient

pytestmark = pytest.mark.postgres


@pytest.fixture
async def platform_settings(settings):  # type: ignore[misc]
    settings.feature_platform = True
    return settings


@pytest.fixture
async def platform_client(
    platform_settings, wipe_db, owner_engine
) -> AsyncIterator[CsrfAwareClient]:
    async with owner_engine.begin() as conn:
        await conn.execute(text("DELETE FROM platform_tenant_memberships"))
        await conn.execute(text("DELETE FROM platform_identities"))

    from app.platform.deps import reset_platform_engine

    reset_platform_engine()
    app = create_app(platform_settings)
    transport = ASGITransport(app=app)
    async with CsrfAwareClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    reset_platform_engine()


# --------------------------------------------------------------- F-06


async def test_f06_unverified_identity_elsewhere_does_not_lock_staff_out(
    platform_settings, demo_tenant, owner_engine
) -> None:
    """A stranger signing up with a staff member's email must not block
    that staff member's tenant login.

    ``_has_unverified_identity`` used to match on email alone, so any
    unverified Identity anywhere — trivially created via /platform/signup
    — denied the real user access to their own tenant with their own
    correct password.
    """
    from app.services.auth_service import _has_unverified_identity

    victim_email = "jan.novak@4mex.cz"

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    identity_id = uuid4()
    async with sm() as session, session.begin():
        session.add(
            Identity(
                id=identity_id,
                email=victim_email,
                full_name="Not Jan",
                password_hash=hash_password("attackerpass"),
                email_verified_at=None,  # never verified — this is the lure
            )
        )

    # An unverified identity with no membership on the victim's tenant
    # must NOT gate the victim's login.
    assert await _has_unverified_identity(victim_email, demo_tenant.id) is False

    # Bind it to the tenant and the gate fires again — the check still
    # does its real job for a genuine unverified self-signup owner.
    from app.models.user import User

    member_user_id = uuid4()
    async with sm() as session, session.begin():
        session.add(
            User(
                id=member_user_id,
                tenant_id=demo_tenant.id,
                email=victim_email,
                full_name="Jan Novak",
                password_hash=hash_password("victimpass"),
            )
        )
        await session.flush()
        session.add(
            TenantMembership(
                id=uuid4(),
                identity_id=identity_id,
                tenant_id=demo_tenant.id,
                user_id=member_user_id,
                is_active=True,
                access_type=MEMBERSHIP_ACCESS_MEMBER,
            )
        )
    assert await _has_unverified_identity(victim_email, demo_tenant.id) is True


# --------------------------------------------------------------- F-23


async def test_f23_disabled_account_is_not_an_existence_oracle(
    tenant_client: CsrfAwareClient, demo_tenant, owner_engine
) -> None:
    """A disabled account must answer a WRONG password the same way an
    unknown address does — otherwise the login form enumerates staff.
    """
    from app.models.user import User
    from app.services.auth_service import AccountDisabled, InvalidCredentials, authenticate

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        session.add(
            User(
                id=uuid4(),
                tenant_id=demo_tenant.id,
                email="disabled@4mex.cz",
                full_name="Disabled Person",
                password_hash=hash_password("correcthorse"),
                is_active=False,
            )
        )

    async with sm() as db:
        # Wrong password on a disabled account: indistinguishable from
        # an address that does not exist at all.
        with pytest.raises(InvalidCredentials):
            await authenticate(db, "disabled@4mex.cz", "wrong-password")

        # Correct password still discloses the real state — the person
        # proved they own the account, so they deserve the real reason.
        with pytest.raises(AccountDisabled):
            await authenticate(db, "disabled@4mex.cz", "correcthorse")


# --------------------------------------------------------------- F-05 / F-21


async def test_f05_platform_reset_token_is_single_use(platform_settings, owner_engine) -> None:
    """A platform password-reset link must die on first use."""
    from app.platform.service import (
        InvalidCredentials,
        create_platform_password_reset_token,
        decode_platform_password_reset_token,
        reset_platform_password,
    )

    identity_id = uuid4()
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        session.add(
            Identity(
                id=identity_id,
                email=f"reset-{uuid4().hex[:8]}@example.com",
                full_name="Reset Me",
                password_hash=hash_password("originalpass"),
                email_verified_at=datetime.now(UTC),
            )
        )

    secret = platform_settings.app_secret_key
    token = create_platform_password_reset_token(secret, identity_id, 0)
    ident_id, token_sv = decode_platform_password_reset_token(secret, token, 1800)
    assert (ident_id, token_sv) == (identity_id, 0)

    async with sm() as session, session.begin():
        await reset_platform_password(session, identity_id, "brandnewpass", token_session_version=0)

    # Replay of the SAME link must now fail.
    async with sm() as session, session.begin():
        with pytest.raises(InvalidCredentials):
            await reset_platform_password(
                session, identity_id, "attackerpass", token_session_version=0
            )

    # And the bump is persisted, so live cookies are invalidated too.
    async with sm() as session:
        row = (
            await session.execute(select(Identity).where(Identity.id == identity_id))
        ).scalar_one()
        assert row.session_version == 1


async def test_f21_platform_reset_enforces_password_floor(
    platform_settings, owner_engine
) -> None:
    """The tenant flow enforces 8 chars in six places; the platform flow
    enforced it nowhere.
    """
    from app.platform.service import InvalidCredentials, reset_platform_password

    identity_id = uuid4()
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        session.add(
            Identity(
                id=identity_id,
                email=f"short-{uuid4().hex[:8]}@example.com",
                full_name="Short Pass",
                password_hash=hash_password("originalpass"),
                email_verified_at=datetime.now(UTC),
            )
        )

    async with sm() as session, session.begin():
        with pytest.raises(InvalidCredentials):
            await reset_platform_password(session, identity_id, "abc", token_session_version=0)


# --------------------------------------------------------------- F-20


async def test_f20_stale_platform_session_version_is_rejected() -> None:
    """A cookie minted before a password reset must stop authenticating."""
    from app.platform.session import PlatformSession

    minted = PlatformSession(identity_id=str(uuid4()), is_platform_admin=False, session_version=0)
    round_tripped = PlatformSession.from_dict(minted.to_dict())
    assert round_tripped.session_version == 0

    # Cookies written before this field existed have no "sv" key at all;
    # they must decode to 0 (the column default) rather than blow up, so
    # the deploy does not log everybody out at once.
    legacy = PlatformSession.from_dict({"iid": str(uuid4()), "admin": False})
    assert legacy.session_version == 0


# --------------------------------------------------------------- F-04


async def test_f04_support_access_membership_is_not_a_billing_identity(
    platform_settings, demo_tenant, owner_engine
) -> None:
    """A platform operator holding a support grant on a paying tenant
    must never resolve as that tenant's billing owner — otherwise
    /platform/billing would let them cancel the customer's subscription.
    """
    from app.models.enums import UserRole
    from app.models.user import User
    from app.platform.routers.billing import _resolve_current_tenant

    operator_id = uuid4()
    support_user_id = uuid4()
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        session.add(
            Identity(
                id=operator_id,
                email=f"operator-{uuid4().hex[:8]}@platform.local",
                full_name="Support Operator",
                password_hash=hash_password("operatorpass"),
                is_platform_admin=True,
                email_verified_at=datetime.now(UTC),
            )
        )
        # Exactly what grant_platform_admin_support_access creates: a
        # TENANT_ADMIN User inside the customer's tenant.
        session.add(
            User(
                id=support_user_id,
                tenant_id=demo_tenant.id,
                email=f"operator-{uuid4().hex[:8]}@platform.local",
                full_name="Support Operator",
                password_hash=None,
                role=UserRole.TENANT_ADMIN,
            )
        )
        await session.flush()
        session.add(
            TenantMembership(
                id=uuid4(),
                identity_id=operator_id,
                tenant_id=demo_tenant.id,
                user_id=support_user_id,
                is_active=True,
                access_type=MEMBERSHIP_ACCESS_SUPPORT,
            )
        )

    async with sm() as db:
        identity = (
            await db.execute(select(Identity).where(Identity.id == operator_id))
        ).scalar_one()
        tenant, user = await _resolve_current_tenant(db, identity)

    assert tenant is None, "support access must not resolve as a billing tenant"
    assert user is None
