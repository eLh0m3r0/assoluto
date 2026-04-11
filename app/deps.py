"""FastAPI dependencies.

Centralises tenant resolution and tenant-scoped DB sessions. Every
application route that touches the database should depend on
`get_db` (or a wrapper that composes with auth checks) so that the
Postgres session variable `app.tenant_id` is always set before the
first query runs — this is what makes Row-Level Security actually
isolate tenants.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.session import get_sessionmaker
from app.models.customer import CustomerContact
from app.models.tenant import Tenant
from app.models.user import User
from app.security.session import SessionData, read_session


def _extract_subdomain(host: str) -> str | None:
    """Return the left-most label of a hostname, if it represents a tenant.

    Handles common dev hostnames:
    - `4mex.portal.example.com` -> `4mex`
    - `4mex.localhost`          -> `4mex`
    - `4mex.localhost:8000`     -> `4mex`
    - `localhost`               -> None
    - `127.0.0.1`               -> None
    """
    if not host:
        return None
    host = host.split(":", 1)[0]  # strip port
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "testserver"):
        return None
    # IPv4 address detection — no subdomain.
    if all(part.isdigit() for part in host.split(".")):
        return None
    parts = host.split(".")
    if len(parts) < 2:
        return None
    first = parts[0]
    # The plain `www` label is never a tenant slug.
    if first in ("www",):
        return None
    return first


def resolve_tenant_slug(request: Request, settings: Settings) -> str | None:
    """Determine which tenant slug a request is addressing.

    Resolution order:
    1. Explicit `X-Tenant-Slug` header (used by tests and internal tools).
    2. Subdomain of the `Host` header.
    3. `DEFAULT_TENANT_SLUG` from settings (self-host single-tenant mode).
    """
    header = request.headers.get("x-tenant-slug")
    if header:
        return header.strip().lower() or None

    host = request.headers.get("host", "")
    subdomain = _extract_subdomain(host)
    if subdomain:
        return subdomain.lower()

    if settings.default_tenant_slug:
        return settings.default_tenant_slug.strip().lower() or None

    return None


async def get_current_tenant(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> Tenant:
    """Resolve the current tenant and ensure it's active.

    Raises HTTP 404 when no tenant can be determined or when the resolved
    slug does not correspond to an active tenant. We deliberately return
    404 rather than 400/401 to avoid leaking which slugs exist.
    """
    slug = resolve_tenant_slug(request, settings)
    if not slug:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    # Look up the tenant using a session WITHOUT any tenant context — the
    # `tenants` table is not RLS-protected, so this is safe.
    sm = get_sessionmaker()
    async with sm() as session:
        result = await session.execute(select(Tenant).where(Tenant.slug == slug))
        tenant = result.scalar_one_or_none()

    if tenant is None or not tenant.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    # Stash on request.state so downstream code (templates, logging) can
    # use it without re-querying.
    request.state.tenant = tenant
    return tenant


async def set_tenant_context(session: AsyncSession, tenant_id: str) -> None:
    """Set the Postgres session variable used by RLS policies.

    Uses the `set_config(name, value, is_local)` function so the value
    can be safely bound as a parameter (plain `SET LOCAL` does not accept
    parameter markers under asyncpg).
    """
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": tenant_id},
    )


async def get_db(
    tenant: Tenant = Depends(get_current_tenant),
) -> AsyncIterator[AsyncSession]:
    """Yield a tenant-scoped async DB session.

    A single transaction wraps the whole request so that `set_config(...,
    is_local=true)` remains in effect for every query issued during the
    request, and all writes are committed or rolled back atomically.
    """
    sm = get_sessionmaker()
    async with sm() as session, session.begin():
        await set_tenant_context(session, str(tenant.id))
        yield session


# ---------------------------------------------------------------------------
# Principal (logged-in user) dependencies
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    """Unified representation of a logged-in caller.

    Abstracts over `User` (tenant staff) and `CustomerContact` (client-side
    user) so endpoints can depend on one thing. Compare `type` when the
    distinction matters.
    """

    type: str  # "user" | "contact"
    id: UUID
    tenant_id: UUID
    customer_id: UUID | None
    full_name: str
    email: str
    role: str
    is_staff: bool
    raw: User | CustomerContact

    @classmethod
    def from_user(cls, user: User) -> Principal:
        return cls(
            type="user",
            id=user.id,
            tenant_id=user.tenant_id,
            customer_id=None,
            full_name=user.full_name,
            email=user.email,
            role=user.role.value,
            is_staff=True,
            raw=user,
        )

    @classmethod
    def from_contact(cls, contact: CustomerContact) -> Principal:
        return cls(
            type="contact",
            id=contact.id,
            tenant_id=contact.tenant_id,
            customer_id=contact.customer_id,
            full_name=contact.full_name,
            email=contact.email,
            role=contact.role.value,
            is_staff=False,
            raw=contact,
        )


async def get_current_principal(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Principal | None:
    """Resolve the caller from the signed session cookie.

    Returns `None` if the cookie is missing, tampered with, expired, points
    at a different tenant, or references a principal that no longer exists
    or was deactivated. Routes that require authentication should depend
    on `require_login` instead of reading `None` from this function.
    """
    session_data: SessionData | None = read_session(request, settings.app_secret_key)
    if session_data is None:
        return None

    # Sanity: cookie must belong to the tenant resolved for this request.
    if session_data.tenant_id != str(tenant.id):
        return None

    try:
        principal_uuid = UUID(session_data.principal_id)
    except ValueError:
        return None

    if session_data.principal_type == "user":
        result = await db.execute(select(User).where(User.id == principal_uuid))
        user = result.scalar_one_or_none()
        if user is None or not user.is_active:
            return None
        if user.session_version != session_data.session_version:
            return None
        return Principal.from_user(user)

    if session_data.principal_type == "contact":
        result = await db.execute(
            select(CustomerContact).where(CustomerContact.id == principal_uuid)
        )
        contact = result.scalar_one_or_none()
        if contact is None or not contact.is_active:
            return None
        if contact.session_version != session_data.session_version:
            return None
        return Principal.from_contact(contact)

    return None


async def require_login(
    principal: Principal | None = Depends(get_current_principal),
) -> Principal:
    """Dependency that 302s unauthenticated callers away via HTTPException.

    Routes that protect user-facing pages should catch 401 in an exception
    handler to redirect to `/auth/login`; API endpoints can just return
    401 directly.
    """
    if principal is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return principal


async def require_tenant_staff(
    principal: Principal = Depends(require_login),
) -> Principal:
    if not principal.is_staff:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant staff required",
        )
    return principal


async def require_customer_contact(
    principal: Principal = Depends(require_login),
) -> Principal:
    if principal.is_staff:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Customer contact required",
        )
    return principal
