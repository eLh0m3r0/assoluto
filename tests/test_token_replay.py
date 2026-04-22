"""Account-recovery tokens must not be replayable.

Covers three classes of tokens that previously admitted a replay
attack if an attacker got their hands on the URL (stolen screenshot,
forwarded email, cached browser history on a shared machine):

* Customer-contact invitation (``accept_invitation``)
* Tenant-staff invitation (``accept_staff_invite``) — was already safe,
  guarded here to prevent regression.
* Password reset (``reset_password_with_token``) — fixed by embedding
  ``session_version`` into the token and rejecting tokens whose
  embedded version no longer matches the principal's current value.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.customer import Customer, CustomerContact
from app.models.enums import CustomerContactRole, UserRole
from app.models.user import User
from app.security.passwords import hash_password
from app.services.auth_service import (
    InvalidInvitation,
    accept_invitation,
    accept_staff_invite,
    create_password_reset_token,
    decode_password_reset_token,
    reset_password_with_token,
)

pytestmark = pytest.mark.postgres

SECRET = "unit-test-secret-key-not-for-prod"


async def _seed_user(owner_engine, tenant_id: UUID) -> User:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        user = User(
            id=uuid4(),
            tenant_id=tenant_id,
            email=f"u-{uuid4().hex[:6]}@4mex.cz",
            full_name="U",
            role=UserRole.TENANT_ADMIN,
            password_hash=hash_password("original-pw-123"),
        )
        session.add(user)
        await session.flush()
        return user


async def _seed_contact(owner_engine, tenant_id: UUID, *, accepted: bool) -> CustomerContact:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        cust = Customer(id=uuid4(), tenant_id=tenant_id, name="C")
        session.add(cust)
        await session.flush()
        contact = CustomerContact(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=cust.id,
            email=f"c-{uuid4().hex[:6]}@acme.cz",
            full_name="C",
            role=CustomerContactRole.CUSTOMER_USER,
            password_hash=hash_password("original-pw-123") if accepted else None,
            invited_at=datetime.now(UTC),
            accepted_at=datetime.now(UTC) if accepted else None,
        )
        session.add(contact)
        await session.flush()
        return contact


async def test_contact_invitation_cannot_be_replayed(owner_engine, demo_tenant) -> None:
    """A captured invitation URL must not reset an already-accepted contact."""
    from sqlalchemy import select

    contact = await _seed_contact(owner_engine, demo_tenant.id, accepted=True)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        with pytest.raises(InvalidInvitation, match="already accepted"):
            await accept_invitation(
                session,
                tenant_id=demo_tenant.id,
                contact_id=contact.id,
                password="attacker-chosen-pw",
            )
    # Confirm the password was NOT changed.
    async with sm() as session:
        fresh = (
            await session.execute(select(CustomerContact).where(CustomerContact.id == contact.id))
        ).scalar_one()
        assert fresh.password_hash == contact.password_hash


async def test_staff_invitation_cannot_be_replayed(owner_engine, demo_tenant) -> None:
    """Guard against regression — staff invite check was already there."""
    from sqlalchemy import select

    user = await _seed_user(owner_engine, demo_tenant.id)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        with pytest.raises(InvalidInvitation, match="already accepted"):
            await accept_staff_invite(
                session,
                tenant_id=demo_tenant.id,
                user_id=user.id,
                password="attacker-chosen-pw",
            )
    async with sm() as session:
        fresh = (
            await session.execute(select(User).where(User.id == user.id))
        ).scalar_one()
        assert fresh.password_hash == user.password_hash


async def test_password_reset_token_is_single_use(owner_engine, demo_tenant) -> None:
    """The token becomes invalid once session_version has bumped."""
    from sqlalchemy import select

    user = await _seed_user(owner_engine, demo_tenant.id)
    token = create_password_reset_token(
        SECRET,
        tenant_id=demo_tenant.id,
        principal_type="user",
        principal_id=user.id,
        session_version=user.session_version,
    )
    _tid, _pt, _pid, token_sv = decode_password_reset_token(
        SECRET, token, max_age_seconds=1800
    )
    assert token_sv == user.session_version

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)

    # First consumption succeeds.
    async with sm() as session, session.begin():
        await reset_password_with_token(
            session,
            tenant_id=demo_tenant.id,
            principal_type="user",
            principal_id=user.id,
            new_password="victim-chosen-pw1",
            token_session_version=token_sv,
        )

    # Second consumption with the same token is rejected.
    async with sm() as session, session.begin():
        with pytest.raises(InvalidInvitation, match="already used"):
            await reset_password_with_token(
                session,
                tenant_id=demo_tenant.id,
                principal_type="user",
                principal_id=user.id,
                new_password="attacker-chosen-pw2",
                token_session_version=token_sv,
            )

    # Confirm the password set by the first legitimate consumer stuck.
    async with sm() as session:
        fresh = (
            await session.execute(select(User).where(User.id == user.id))
        ).scalar_one()
        # Hash differs from the original AND from the attacker attempt.
        assert fresh.password_hash != user.password_hash
        from app.security.passwords import verify_password
        assert verify_password("victim-chosen-pw1", fresh.password_hash)
        assert not verify_password("attacker-chosen-pw2", fresh.password_hash)


async def test_password_reset_invalidated_by_unrelated_session_bump(
    owner_engine, demo_tenant
) -> None:
    """A reset token minted before a password change elsewhere is void.

    Example attack: attacker briefly had the user's email, requested a
    reset, held onto the token. User notices, changes their password
    via /app/admin/profile (which bumps session_version). Attacker's
    pre-existing reset token must now be rejected.
    """
    user = await _seed_user(owner_engine, demo_tenant.id)
    token = create_password_reset_token(
        SECRET,
        tenant_id=demo_tenant.id,
        principal_type="user",
        principal_id=user.id,
        session_version=user.session_version,
    )
    _tid, _pt, _pid, token_sv = decode_password_reset_token(
        SECRET, token, max_age_seconds=1800
    )

    # Simulate an unrelated session_version bump (manual password change).
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        from sqlalchemy import select
        fresh = (
            await session.execute(select(User).where(User.id == user.id))
        ).scalar_one()
        fresh.session_version += 1

    async with sm() as session, session.begin():
        with pytest.raises(InvalidInvitation, match="already used"):
            await reset_password_with_token(
                session,
                tenant_id=demo_tenant.id,
                principal_type="user",
                principal_id=user.id,
                new_password="attacker-pw",
                token_session_version=token_sv,
            )
