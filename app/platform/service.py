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
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer, CustomerContact
from app.models.enums import UserRole
from app.models.tenant import Tenant
from app.models.user import User
from app.platform.models import Identity, TenantMembership
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
) -> Identity:
    """Return an existing Identity matching `email`, or create a new one.

    `password` is optional — when None, a pending-state Identity with an
    empty password is written and must be finalized via an invite
    acceptance flow (same pattern as staff invites).
    """
    identity = await find_identity_by_email(db, email)
    if identity is not None:
        return identity

    identity = Identity(
        id=uuid4(),
        email=email.strip().lower(),
        full_name=full_name.strip() or email,
        password_hash=hash_password(password) if password else "",
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
) -> tuple[Tenant, User]:
    """Create a new Tenant and seed its first admin user.

    Also ensures a platform Identity exists for the owner email and
    links the two via TenantMembership so the owner can log in through
    /platform/login AND directly on the tenant subdomain.
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
    )
    await link_user_to_identity(db, user=owner, identity=identity)

    return tenant, owner


async def deactivate_tenant(db: AsyncSession, *, tenant_id: UUID) -> None:
    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if tenant is None:
        raise PlatformError("tenant not found")
    tenant.is_active = False
    await db.flush()


# -------------------------------------------------------- self-signup flow


async def signup_tenant(
    db: AsyncSession,
    *,
    company_name: str,
    slug: str,
    owner_email: str,
    owner_full_name: str,
    owner_password: str,
) -> tuple[Tenant, User, Identity]:
    """Create a tenant + admin user + Identity for the self-signup flow.

    Same as :func:`create_tenant_with_owner` but also:

    * enforces that the Identity email is globally unique (you cannot
      signup twice with the same address)
    * stamps ``terms_accepted_at`` on the new Identity

    Caller is responsible for sending the verification email afterwards.
    """
    # Fail fast if the email already has a platform Identity.
    existing = await find_identity_by_email(db, owner_email)
    if existing is not None:
        raise DuplicateIdentityEmail(owner_email.strip().lower())

    tenant, owner = await create_tenant_with_owner(
        db,
        slug=slug,
        name=company_name,
        owner_email=owner_email,
        owner_full_name=owner_full_name,
        owner_password=owner_password,
    )
    # `create_tenant_with_owner` already created/linked the Identity.
    identity = await find_identity_by_email(db, owner_email)
    assert identity is not None  # just created above
    identity.terms_accepted_at = datetime.now(UTC)
    await db.flush()

    # Give the new tenant a 14-day trial on the starter plan by default.
    # Wrapped in a try so a missing plan row (which would only happen if
    # migration 1003 hasn't been applied) doesn't break signup.
    try:
        from app.platform.billing.service import start_trial_subscription

        await start_trial_subscription(db, tenant=tenant, plan_code="starter")
    except Exception:
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
