"""Platform-level business logic.

Everything in here uses the OWNER async engine (bypassing RLS) because
platform operations need to span multiple tenants. Callers must never
pass these sessions to tenant-scoped code expecting `app.tenant_id` to
be set.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer, CustomerContact
from app.models.enums import UserRole
from app.models.tenant import Tenant
from app.models.user import User
from app.platform.models import (
    MEMBERSHIP_ACCESS_SUPPORT,
    Identity,
    TenantMembership,
)
from app.security.passwords import hash_password, verify_password


class PlatformError(Exception):
    pass


class InvalidCredentials(PlatformError):
    pass


class AccountDisabled(PlatformError):
    pass


class DuplicateTenantSlug(PlatformError):
    pass


class DuplicateIdentityEmail(PlatformError):
    pass


# ---------------------------------------------------------- identity helpers


async def find_identity_by_email(db: AsyncSession, email: str) -> Identity | None:
    email = email.strip().lower()
    if not email:
        return None
    return (await db.execute(select(Identity).where(Identity.email == email))).scalar_one_or_none()


async def create_or_get_identity(
    db: AsyncSession,
    *,
    email: str,
    full_name: str,
    password: str | None = None,
    pre_verified: bool = False,
) -> Identity:
    """Return an existing Identity matching `email`, or create a new one.

    ``password`` is optional — when ``None``, a pending-state Identity
    with an empty password is written and must be finalized via an
    invite-acceptance flow (same pattern as staff invites).

    ``pre_verified`` (default ``False``) stamps ``email_verified_at`` at
    creation time. Use it only when the identity is being provisioned
    by a trusted channel — platform admin creating a tenant, CLI
    bootstrap, seed script — otherwise the signup flow owns the
    email-verification handshake. Round-3 audit Backend-P2 fix:
    admin-created identities now skip the verify gate because they
    can't ever click the self-service verification link (nobody sends
    them one).
    """
    identity = await find_identity_by_email(db, email)
    if identity is not None:
        return identity

    identity = Identity(
        id=uuid4(),
        email=email.strip().lower(),
        full_name=full_name.strip() or email,
        password_hash=hash_password(password) if password else "",
        email_verified_at=datetime.now(UTC) if pre_verified else None,
    )
    db.add(identity)
    await db.flush()
    return identity


async def authenticate_identity(db: AsyncSession, email: str, password: str) -> Identity:
    identity = await find_identity_by_email(db, email)
    if identity is None or not identity.password_hash:
        raise InvalidCredentials()
    if not identity.is_active:
        raise AccountDisabled()
    if not verify_password(password, identity.password_hash):
        raise InvalidCredentials()
    identity.last_login_at = datetime.now(UTC)
    await db.flush()
    return identity


def create_platform_password_reset_token(secret_key: str, identity_id: UUID) -> str:
    from app.security.tokens import TokenPurpose, create_token

    return create_token(
        secret_key,
        TokenPurpose.PLATFORM_PASSWORD_RESET,
        {"identity_id": str(identity_id)},
    )


def decode_platform_password_reset_token(secret_key: str, token: str, max_age_seconds: int) -> UUID:
    from app.security.tokens import TokenPurpose, verify_token

    try:
        data = verify_token(
            secret_key, TokenPurpose.PLATFORM_PASSWORD_RESET, token, max_age_seconds
        )
    except Exception as exc:
        raise InvalidCredentials(str(exc)) from exc
    return UUID(data["identity_id"])


async def reset_platform_password(db: AsyncSession, identity_id: UUID, new_password: str) -> None:
    identity = (
        await db.execute(select(Identity).where(Identity.id == identity_id))
    ).scalar_one_or_none()
    if identity is None or not identity.is_active:
        raise InvalidCredentials("Identity not found or disabled")
    identity.password_hash = hash_password(new_password)
    await db.flush()


# --------------------------------------------------------- membership helpers


async def link_contact_to_identity(
    db: AsyncSession, *, contact: CustomerContact, identity: Identity
) -> TenantMembership:
    """Ensure a membership row exists linking `identity` to `contact`.

    Idempotent — returns the existing row if one is already there.
    """
    existing = (
        await db.execute(
            select(TenantMembership).where(
                TenantMembership.identity_id == identity.id,
                TenantMembership.tenant_id == contact.tenant_id,
                TenantMembership.contact_id == contact.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    membership = TenantMembership(
        id=uuid4(),
        identity_id=identity.id,
        tenant_id=contact.tenant_id,
        user_id=None,
        contact_id=contact.id,
    )
    db.add(membership)
    await db.flush()
    return membership


async def link_user_to_identity(
    db: AsyncSession, *, user: User, identity: Identity
) -> TenantMembership:
    existing = (
        await db.execute(
            select(TenantMembership).where(
                TenantMembership.identity_id == identity.id,
                TenantMembership.tenant_id == user.tenant_id,
                TenantMembership.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    membership = TenantMembership(
        id=uuid4(),
        identity_id=identity.id,
        tenant_id=user.tenant_id,
        user_id=user.id,
        contact_id=None,
    )
    db.add(membership)
    await db.flush()
    return membership


async def list_memberships_for_identity(
    db: AsyncSession, *, identity_id: UUID
) -> list[TenantMembership]:
    result = await db.execute(
        select(TenantMembership).where(
            TenantMembership.identity_id == identity_id,
            TenantMembership.is_active.is_(True),
        )
    )
    return list(result.scalars().all())


async def resolve_membership_targets(
    db: AsyncSession, *, membership: TenantMembership
) -> tuple[Tenant, User | CustomerContact | None]:
    tenant = (
        await db.execute(select(Tenant).where(Tenant.id == membership.tenant_id))
    ).scalar_one()
    target: User | CustomerContact | None = None
    if membership.user_id is not None:
        target = (
            await db.execute(select(User).where(User.id == membership.user_id))
        ).scalar_one_or_none()
    elif membership.contact_id is not None:
        target = (
            await db.execute(
                select(CustomerContact).where(CustomerContact.id == membership.contact_id)
            )
        ).scalar_one_or_none()
    return tenant, target


# -------------------------------------------------------- tenant provisioning


async def list_tenants(db: AsyncSession) -> list[Tenant]:
    return list((await db.execute(select(Tenant).order_by(Tenant.slug))).scalars().all())


async def create_tenant_with_owner(
    db: AsyncSession,
    *,
    slug: str,
    name: str,
    owner_email: str,
    owner_full_name: str,
    owner_password: str,
    pre_verified_identity: bool = False,
) -> tuple[Tenant, User]:
    """Create a new Tenant and seed its first admin user.

    Also ensures a platform Identity exists for the owner email and
    links the two via TenantMembership so the owner can log in through
    /platform/login AND directly on the tenant subdomain.

    ``pre_verified_identity`` (default ``False``): when True, the
    newly-minted Identity is stamped with ``email_verified_at`` so it
    skips the verification gate. Only set this from trusted
    provisioning paths (platform admin creating a tenant on behalf
    of a customer, CLI bootstrap) — the self-signup flow MUST leave
    it False and rely on the verification email.
    """
    slug = slug.strip().lower()
    if not slug:
        raise PlatformError("slug is required")

    dup = (await db.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
    if dup is not None:
        raise DuplicateTenantSlug(slug)

    tenant = Tenant(
        id=uuid4(),
        slug=slug,
        name=name.strip() or slug,
        billing_email=owner_email.strip().lower(),
        storage_prefix=f"tenants/{slug}/",
    )
    db.add(tenant)
    await db.flush()

    owner = User(
        id=uuid4(),
        tenant_id=tenant.id,
        email=owner_email.strip().lower(),
        full_name=owner_full_name.strip() or owner_email,
        password_hash=hash_password(owner_password),
        role=UserRole.TENANT_ADMIN,
    )
    db.add(owner)
    await db.flush()

    identity = await create_or_get_identity(
        db,
        email=owner_email,
        full_name=owner_full_name or owner_email,
        password=owner_password,
        pre_verified=pre_verified_identity,
    )
    await link_user_to_identity(db, user=owner, identity=identity)

    return tenant, owner


async def deactivate_tenant(db: AsyncSession, *, tenant_id: UUID) -> None:
    """Flip ``is_active`` to False AND invalidate every existing session.

    Without bumping ``session_version`` on every user + customer contact
    in the tenant, anyone with an active browser session keeps their
    cookie working — they just hit a 404 on the next request, but the
    cookie itself remains valid until expiry. For the
    suspended-for-non-payment scenario this is the wrong default.
    Bumping the version forces re-authentication on the next request.
    """
    from sqlalchemy import text

    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if tenant is None:
        raise PlatformError("tenant not found")
    tenant.is_active = False
    # Bump session_version on staff users + customer contacts so any
    # in-flight browser session immediately fails next request.
    # ORM update_all would re-fetch each row; raw text() is one round-trip.
    await db.execute(
        text("UPDATE users SET session_version = session_version + 1 WHERE tenant_id = :tid"),
        {"tid": tenant_id},
    )
    await db.execute(
        text(
            "UPDATE customer_contacts SET session_version = session_version + 1 "
            "WHERE tenant_id = :tid"
        ),
        {"tid": tenant_id},
    )
    await db.flush()


async def reactivate_tenant(db: AsyncSession, *, tenant_id: UUID) -> None:
    """Flip ``is_active`` back to True — inverse of ``deactivate_tenant``.

    Deactivation only toggles the flag, so a simple flip restores login
    and subdomain access. No data is touched.
    """
    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if tenant is None:
        raise PlatformError("tenant not found")
    tenant.is_active = True
    await db.flush()


async def update_tenant(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    name: str | None = None,
    billing_email: str | None = None,
) -> Tenant:
    """Edit tenant display fields from the platform admin UI.

    ``slug`` is deliberately immutable — it's baked into the storage
    prefix, subdomain DNS, and any bookmarks customers already use.
    Renaming a slug is effectively a new tenant; go through a full
    migration, not a silent rename.
    """
    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if tenant is None:
        raise PlatformError("tenant not found")
    if name is not None:
        cleaned = name.strip()
        if not cleaned:
            raise PlatformError("name cannot be empty")
        tenant.name = cleaned
    if billing_email is not None:
        cleaned_email = billing_email.strip().lower()
        if not cleaned_email:
            raise PlatformError("billing_email cannot be empty")
        tenant.billing_email = cleaned_email
    await db.flush()
    return tenant


async def grant_platform_admin_support_access(
    db: AsyncSession,
    *,
    identity: Identity,
    tenant_id: UUID,
) -> tuple[User, TenantMembership]:
    """Materialise a platform admin's *support* access to a tenant.

    Creates (or reuses) a ``User`` row inside the target tenant with
    the platform admin's email + ``role=TENANT_ADMIN`` (password_hash
    NULL — login is only via platform-session handoff), plus a
    ``TenantMembership`` row linking the platform identity to that
    user. The platform admin then sees the tenant in
    ``/platform/select-tenant`` and uses the normal switch flow — no
    special impersonation path is required anywhere else in the app.

    Idempotent: repeated calls return the existing rows unchanged.
    """
    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if tenant is None:
        raise PlatformError("tenant not found")
    if not tenant.is_active:
        raise PlatformError("tenant is deactivated — reactivate first")
    if not identity.is_platform_admin:
        raise PlatformError("identity is not a platform admin")

    email_lower = identity.email.lower()
    existing_user = (
        await db.execute(select(User).where(User.tenant_id == tenant_id, User.email == email_lower))
    ).scalar_one_or_none()

    if existing_user is None:
        user = User(
            tenant_id=tenant_id,
            email=email_lower,
            full_name=email_lower,
            password_hash=None,
            role=UserRole.TENANT_ADMIN,
        )
        db.add(user)
        await db.flush()
    else:
        user = existing_user
        if not user.is_active:
            user.is_active = True
            await db.flush()

    existing_membership = (
        await db.execute(
            select(TenantMembership).where(
                TenantMembership.identity_id == identity.id,
                TenantMembership.tenant_id == tenant_id,
                TenantMembership.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if existing_membership is not None:
        # Reactivate + mark as support if it was previously revoked or
        # created as a regular membership (shouldn't happen in normal
        # flow, but keep the invariant tight).
        if not existing_membership.is_active:
            existing_membership.is_active = True
        existing_membership.access_type = MEMBERSHIP_ACCESS_SUPPORT
        await db.flush()
        return user, existing_membership

    membership = TenantMembership(
        id=uuid4(),
        identity_id=identity.id,
        tenant_id=tenant_id,
        user_id=user.id,
        contact_id=None,
        access_type=MEMBERSHIP_ACCESS_SUPPORT,
    )
    db.add(membership)
    await db.flush()
    return user, membership


async def revoke_platform_admin_support_access(
    db: AsyncSession,
    *,
    identity: Identity,
    tenant_id: UUID,
) -> tuple[User, TenantMembership] | None:
    """Undo ``grant_platform_admin_support_access``.

    Removes the ``TenantMembership(access_type="support")`` row for the
    given platform admin + tenant and deactivates the matching User row
    (kept for foreign-key integrity with ``order_comments.author_user_id``
    and friends — hard-delete would orphan every audit trail the support
    admin produced while in the tenant).

    Returns ``(user, membership)`` on success or ``None`` if nothing to
    revoke (no support membership exists). Idempotent.
    """
    membership = (
        await db.execute(
            select(TenantMembership).where(
                TenantMembership.identity_id == identity.id,
                TenantMembership.tenant_id == tenant_id,
                TenantMembership.access_type == MEMBERSHIP_ACCESS_SUPPORT,
            )
        )
    ).scalar_one_or_none()
    if membership is None:
        return None

    user = None
    if membership.user_id is not None:
        user = (
            await db.execute(select(User).where(User.id == membership.user_id))
        ).scalar_one_or_none()

    # Wipe the membership first so a re-grant can fall back to the
    # clean-insert path without tripping the composite unique
    # constraint on (identity, tenant, user, contact).
    await db.delete(membership)
    if user is not None and user.is_active:
        user.is_active = False
    await db.flush()
    return user, membership  # type: ignore[return-value]


# -------------------------------------------------------- self-signup flow


async def signup_tenant(
    db: AsyncSession,
    *,
    company_name: str,
    slug: str,
    owner_email: str,
    owner_full_name: str,
    owner_password: str,
    consent_ip: str | None = None,
    consent_version: str | None = None,
) -> tuple[Tenant, User, Identity]:
    """Create a tenant + admin user + Identity for the self-signup flow.

    Same as :func:`create_tenant_with_owner` but also:

    * enforces that the Identity email is globally unique (you cannot
      signup twice with the same address)
    * stamps ``terms_accepted_at`` on the new Identity

    Caller is responsible for sending the verification email afterwards.
    """
    # Fail fast if the email already has a platform Identity. This is the
    # happy-path check; the IntegrityError catch below handles the race
    # where two concurrent requests both pass this check and only one of
    # them wins the unique-constraint lottery at flush-time.
    existing = await find_identity_by_email(db, owner_email)
    if existing is not None:
        raise DuplicateIdentityEmail(owner_email.strip().lower())

    try:
        tenant, owner = await create_tenant_with_owner(
            db,
            slug=slug,
            name=company_name,
            owner_email=owner_email,
            owner_full_name=owner_full_name,
            owner_password=owner_password,
        )
    except IntegrityError as exc:
        # Translate DB-level unique-constraint violations to the same
        # domain exceptions the happy-path code already raises.
        await db.rollback()
        msg = str(getattr(exc, "orig", exc))
        if "platform_identities_email" in msg:
            raise DuplicateIdentityEmail(owner_email.strip().lower()) from exc
        if "tenants_slug" in msg or "uq_tenants_slug" in msg:
            raise DuplicateTenantSlug(slug) from exc
        raise

    # `create_tenant_with_owner` already created/linked the Identity.
    identity = await find_identity_by_email(db, owner_email)
    assert identity is not None  # just created above
    identity.terms_accepted_at = datetime.now(UTC)
    # Record which version the user ticked + from where, for GDPR
    # consent-proof. Truncate to DB column widths so an unexpectedly
    # long IPv6 literal or a bumped version string doesn't trip the
    # VARCHAR limit — the GDPR value is "we have SOMETHING", not
    # "we have the full byte-perfect match".
    if consent_version:
        identity.terms_accepted_version = consent_version[:32]
    if consent_ip:
        identity.terms_accepted_ip = consent_ip[:45]
    await db.flush()

    # Give the new tenant a 14-day trial on the starter plan by default.
    # The only known failure mode here is "migration 1003 hasn't been
    # applied yet" (PlanNotFound). Anything else indicates a real bug
    # and should surface rather than silently continue.
    try:
        from app.platform.billing.service import PlanNotFound, start_trial_subscription

        await start_trial_subscription(db, tenant=tenant, plan_code="starter")
    except PlanNotFound:
        pass

    return tenant, owner, identity


async def mark_email_verified(db: AsyncSession, *, identity_id: UUID) -> Identity:
    """Mark an Identity as email-verified. Idempotent."""
    identity = (
        await db.execute(select(Identity).where(Identity.id == identity_id))
    ).scalar_one_or_none()
    if identity is None:
        raise PlatformError("identity not found")
    if identity.email_verified_at is None:
        identity.email_verified_at = datetime.now(UTC)
        await db.flush()
    return identity


# ----------------------------------------------------------- customer lookup


async def list_customer_name_for_contact(db: AsyncSession, contact_id: UUID) -> str | None:
    """Return the customer name for a contact-scoped membership."""
    row = (
        await db.execute(
            select(Customer.name)
            .join(CustomerContact, Customer.id == CustomerContact.customer_id)
            .where(CustomerContact.id == contact_id)
        )
    ).scalar_one_or_none()
    return row
