"""Authentication business logic: login, logout, invitations, password reset.

Routes in `app.routers.public` and `app.routers.tenant_admin` call into
this module. Keeping HTTP-free helpers here makes them trivially unit-
testable with a fake DB session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer, CustomerContact
from app.models.enums import CustomerContactRole, UserRole
from app.models.user import User
from app.security.passwords import hash_password, needs_rehash, verify_password
from app.security.tokens import (
    ExpiredToken,
    InvalidToken,
    TokenPurpose,
    create_token,
    verify_token,
)


class AuthError(Exception):
    """Base class for auth business errors (mapped to HTTP by routers)."""


class InvalidCredentials(AuthError):
    pass


class AccountDisabled(AuthError):
    pass


class InvalidInvitation(AuthError):
    pass


@dataclass(frozen=True)
class LoginResult:
    principal_type: str  # "user" | "contact"
    principal_id: UUID
    tenant_id: UUID
    customer_id: UUID | None
    full_name: str
    email: str
    session_version: int


async def authenticate(
    db: AsyncSession,
    email: str,
    password: str,
) -> LoginResult:
    """Verify credentials against both `users` and `customer_contacts`.

    The current tenant context is already set on `db` (via RLS), so we
    only see rows for the right tenant.
    """
    email = email.strip().lower()
    if not email or not password:
        raise InvalidCredentials("email and password required")

    # Try tenant staff first.
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is not None:
        if not user.is_active:
            raise AccountDisabled()
        if not verify_password(password, user.password_hash):
            raise InvalidCredentials()

        # Opportunistically refresh stale hashes.
        if user.password_hash and needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)

        user.last_login_at = datetime.now(UTC)
        await db.flush()

        return LoginResult(
            principal_type="user",
            principal_id=user.id,
            tenant_id=user.tenant_id,
            customer_id=None,
            full_name=user.full_name,
            email=user.email,
            session_version=user.session_version,
        )

    # Otherwise look for a customer contact.
    contact = (
        await db.execute(select(CustomerContact).where(CustomerContact.email == email))
    ).scalar_one_or_none()
    if contact is None:
        raise InvalidCredentials()
    if not contact.is_active:
        raise AccountDisabled()
    if contact.accepted_at is None:
        raise InvalidCredentials("invitation not accepted yet")
    if not verify_password(password, contact.password_hash):
        raise InvalidCredentials()

    if contact.password_hash and needs_rehash(contact.password_hash):
        contact.password_hash = hash_password(password)

    await db.flush()

    return LoginResult(
        principal_type="contact",
        principal_id=contact.id,
        tenant_id=contact.tenant_id,
        customer_id=contact.customer_id,
        full_name=contact.full_name,
        email=contact.email,
        session_version=contact.session_version,
    )


# ---------------------------------------------------------------- invites


def create_invitation_token(secret_key: str, *, tenant_id: UUID, contact_id: UUID) -> str:
    """Return a signed token embedding tenant + contact IDs."""
    return create_token(
        secret_key,
        TokenPurpose.INVITE,
        {"tid": str(tenant_id), "cid": str(contact_id)},
    )


def decode_invitation_token(
    secret_key: str, token: str, *, max_age_seconds: int
) -> tuple[UUID, UUID]:
    """Return `(tenant_id, contact_id)` or raise `InvalidInvitation`."""
    try:
        payload = verify_token(secret_key, TokenPurpose.INVITE, token, max_age_seconds)
    except (InvalidToken, ExpiredToken) as exc:
        raise InvalidInvitation(str(exc)) from exc
    try:
        return UUID(payload["tid"]), UUID(payload["cid"])
    except (KeyError, ValueError) as exc:
        raise InvalidInvitation("malformed payload") from exc


async def accept_invitation(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    contact_id: UUID,
    password: str,
) -> CustomerContact:
    """Finalise an invitation by setting the password and `accepted_at`."""
    contact = (
        await db.execute(select(CustomerContact).where(CustomerContact.id == contact_id))
    ).scalar_one_or_none()

    if contact is None:
        raise InvalidInvitation("unknown contact")
    if contact.tenant_id != tenant_id:
        raise InvalidInvitation("tenant mismatch")
    if not contact.is_active:
        raise InvalidInvitation("contact disabled")

    if len(password) < 8:
        raise InvalidInvitation("password must be at least 8 characters")

    contact.password_hash = hash_password(password)
    contact.accepted_at = datetime.now(UTC)
    contact.session_version += 1  # invalidate any pre-existing sessions
    await db.flush()
    return contact


async def invite_customer_contact(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    customer_id: UUID,
    email: str,
    full_name: str,
    phone: str | None = None,
    role: CustomerContactRole = CustomerContactRole.CUSTOMER_USER,
) -> CustomerContact:
    """Create a new contact row in `invited` state.

    The caller is responsible for generating the signed token and
    enqueueing the email via BackgroundTasks.
    """
    # Validate that the target customer exists in the current tenant.
    customer = (
        await db.execute(select(Customer).where(Customer.id == customer_id))
    ).scalar_one_or_none()
    if customer is None:
        raise InvalidInvitation("unknown customer")

    contact = CustomerContact(
        tenant_id=tenant_id,
        customer_id=customer_id,
        email=email.strip().lower(),
        full_name=full_name.strip(),
        phone=phone,
        role=role,
        invited_at=datetime.now(UTC),
    )
    db.add(contact)
    await db.flush()
    return contact


# ------------------------------------------------------- staff user helper


async def create_tenant_user(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    email: str,
    full_name: str,
    password: str,
    role: UserRole = UserRole.TENANT_STAFF,
) -> User:
    """Create an active tenant staff user with a hashed password."""
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")

    user = User(
        tenant_id=tenant_id,
        email=email.strip().lower(),
        full_name=full_name.strip(),
        password_hash=hash_password(password),
        role=role,
    )
    db.add(user)
    await db.flush()
    return user
