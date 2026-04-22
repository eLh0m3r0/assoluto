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
from app.services import audit_service
from app.services.audit_service import SYSTEM_ACTOR, ActorInfo


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
    # Invitation tokens are valid for 7 days. Once accepted they must
    # NOT be replayable — otherwise an attacker who grabs the link out
    # of an email (shared inbox, forwarded screenshot, browser history
    # on a shared computer) can overwrite the contact's password and
    # take over the account. Mirrors the same guard on accept_staff_invite.
    if contact.accepted_at is not None:
        raise InvalidInvitation("invitation already accepted")

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
    audit_actor: ActorInfo | None = None,
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
    await audit_service.record(
        db,
        action="user.invited",
        entity_type="user",
        entity_id=user.id,
        entity_label=user.email,
        actor=audit_actor or SYSTEM_ACTOR,
        after={"email": user.email, "role": user.role.value},
        tenant_id=tenant_id,
    )
    return user


# ----------------------------------------------------- staff invite flow


def create_staff_invite_token(secret_key: str, *, tenant_id: UUID, user_id: UUID) -> str:
    """Return a signed token embedding tenant + user IDs for staff accept."""
    return create_token(
        secret_key,
        TokenPurpose.STAFF_INVITE,
        {"tid": str(tenant_id), "uid": str(user_id)},
    )


def decode_staff_invite_token(
    secret_key: str, token: str, *, max_age_seconds: int
) -> tuple[UUID, UUID]:
    try:
        payload = verify_token(secret_key, TokenPurpose.STAFF_INVITE, token, max_age_seconds)
    except (InvalidToken, ExpiredToken) as exc:
        raise InvalidInvitation(str(exc)) from exc
    try:
        return UUID(payload["tid"]), UUID(payload["uid"])
    except (KeyError, ValueError) as exc:
        raise InvalidInvitation("malformed payload") from exc


async def invite_tenant_staff(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    email: str,
    full_name: str,
    role: UserRole = UserRole.TENANT_STAFF,
    audit_actor: ActorInfo | None = None,
) -> User:
    """Create a staff user in an inactive-until-accepted state.

    The row is inserted with `password_hash=NULL`; the recipient sets it
    themselves via `/invite/staff?token=...`. `is_active` stays True so
    the subsequent login works right after accept.
    """
    user = User(
        tenant_id=tenant_id,
        email=email.strip().lower(),
        full_name=full_name.strip(),
        password_hash=None,
        role=role,
    )
    db.add(user)
    await db.flush()
    await audit_service.record(
        db,
        action="user.invited",
        entity_type="user",
        entity_id=user.id,
        entity_label=user.email,
        actor=audit_actor or SYSTEM_ACTOR,
        after={"email": user.email, "role": user.role.value},
        tenant_id=tenant_id,
    )
    return user


async def accept_staff_invite(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
    password: str,
) -> User:
    """Finalise a staff invitation by setting the password."""
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise InvalidInvitation("unknown user")
    if user.tenant_id != tenant_id:
        raise InvalidInvitation("tenant mismatch")
    if not user.is_active:
        raise InvalidInvitation("user disabled")
    if user.password_hash is not None:
        # Re-accepting a completed invite would reset the password silently
        # from whoever grabbed the URL — block that.
        raise InvalidInvitation("invitation already accepted")
    if len(password) < 8:
        raise InvalidInvitation("password must be at least 8 characters")

    user.password_hash = hash_password(password)
    user.session_version += 1
    await db.flush()
    return user


async def change_user_password(
    db: AsyncSession,
    *,
    user: User,
    current_password: str,
    new_password: str,
    audit_actor: ActorInfo | None = None,
) -> User:
    """Self-service password change for tenant staff.

    Bumps `session_version` so any other logged-in sessions are
    invalidated on the next request.
    """
    if not verify_password(current_password, user.password_hash):
        raise InvalidCredentials("current password is incorrect")
    if len(new_password) < 8:
        raise InvalidCredentials("new password must be at least 8 characters")
    user.password_hash = hash_password(new_password)
    user.session_version += 1
    await db.flush()
    await audit_service.record(
        db,
        action="user.password_changed",
        entity_type="user",
        entity_id=user.id,
        entity_label=user.email,
        actor=audit_actor or SYSTEM_ACTOR,
        tenant_id=user.tenant_id,
    )
    return user


# ---------------------------------------------------- password reset flow


def create_password_reset_token(
    secret_key: str,
    *,
    tenant_id: UUID,
    principal_type: str,
    principal_id: UUID,
    session_version: int,
) -> str:
    """Mint a password-reset token bound to the principal's current
    ``session_version`` so the token is single-use.

    On consumption the service bumps ``session_version`` — a second
    attempt with the same token then sees ``token.sv != row.sv`` and
    is rejected. Also, any unrelated event that bumps ``session_version``
    (a login-change, a manual admin reset, or the user accepting a
    fresh invitation) invalidates outstanding reset tokens automatically.
    """
    return create_token(
        secret_key,
        TokenPurpose.PASSWORD_RESET,
        {
            "tid": str(tenant_id),
            "pt": principal_type,
            "pid": str(principal_id),
            "sv": int(session_version),
        },
    )


def decode_password_reset_token(
    secret_key: str, token: str, *, max_age_seconds: int
) -> tuple[UUID, str, UUID, int]:
    try:
        payload = verify_token(secret_key, TokenPurpose.PASSWORD_RESET, token, max_age_seconds)
    except (InvalidToken, ExpiredToken) as exc:
        raise InvalidInvitation(str(exc)) from exc
    try:
        return (
            UUID(payload["tid"]),
            str(payload["pt"]),
            UUID(payload["pid"]),
            int(payload.get("sv", 0)),
        )
    except (KeyError, ValueError) as exc:
        raise InvalidInvitation("malformed payload") from exc


async def find_principal_by_email(
    db: AsyncSession, email: str
) -> tuple[str, User | CustomerContact] | None:
    """Return ("user"|"contact", row) matching the e-mail in current tenant."""
    email = email.strip().lower()
    if not email:
        return None
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is not None and user.is_active:
        return "user", user
    contact = (
        await db.execute(select(CustomerContact).where(CustomerContact.email == email))
    ).scalar_one_or_none()
    if contact is not None and contact.is_active:
        return "contact", contact
    return None


async def reset_password_with_token(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    principal_type: str,
    principal_id: UUID,
    new_password: str,
    token_session_version: int,
) -> None:
    """Set a new password for the target principal + bump session_version.

    Rejects a token whose embedded ``session_version`` no longer matches
    the principal's current value — this is what makes the reset link
    one-shot. A successful call bumps ``session_version``, so a second
    call with the same token sees a mismatch and raises.
    """
    if len(new_password) < 8:
        raise InvalidInvitation("password must be at least 8 characters")
    if principal_type == "user":
        row = (await db.execute(select(User).where(User.id == principal_id))).scalar_one_or_none()
    elif principal_type == "contact":
        row = (
            await db.execute(select(CustomerContact).where(CustomerContact.id == principal_id))
        ).scalar_one_or_none()
    else:
        raise InvalidInvitation("bad principal type")

    if row is None or row.tenant_id != tenant_id or not row.is_active:
        raise InvalidInvitation("unknown principal")

    # Token is bound to the session_version at mint time. If anything
    # bumped it since (a previous reset, a password change, a contact
    # accepting their invite), this token is stale and must not work.
    if row.session_version != token_session_version:
        raise InvalidInvitation("token already used or superseded")

    row.password_hash = hash_password(new_password)
    row.session_version += 1
    if isinstance(row, CustomerContact) and row.accepted_at is None:
        row.accepted_at = datetime.now(UTC)
    await db.flush()
