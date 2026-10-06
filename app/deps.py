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
from app.models.customer import Customer, CustomerContact
from app.models.tenant import Tenant
from app.models.user import User
from app.security.session import SessionData, read_session
from app.timezones import tenant_tz


def _extract_subdomain(host: str) -> str | None:
    """Return the left-most label of a hostname, if it represents a tenant.

    Handles common hostnames:
    - `4mex.portal.example.com` -> `4mex`
    - `4mex.localhost`          -> `4mex`
    - `4mex.localhost:8000`     -> `4mex`
    - `assoluto.eu` (apex)      -> None   (only 2 labels → no subdomain)
    - `localhost`               -> None
    - `127.0.0.1`               -> None

    A registered domain typically has the form ``<SLD>.<TLD>`` (two
    labels). Anything longer implies at least one subdomain label in
    front, which is what identifies the tenant. Without this 2-label
    cutoff ``assoluto.eu`` would resolve slug=``assoluto`` and every
    apex hit would 404 "tenant not found" — hiding the platform
    landing page.

    The test-friendly single-label hostnames (``*.localhost``) still
    work because the host there is literally ``4mex.localhost`` — two
    labels where the second IS effectively a TLD. The 2-label cutoff
    excludes those too, so we special-case ``.localhost`` below.
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
    # ``<subdomain>.localhost`` is a dev convention — treat the first
    # label as the tenant slug regardless of label count.
    if parts[-1] == "localhost":
        return first
    # Registered domain ``<SLD>.<TLD>`` has exactly two labels and no
    # tenant subdomain. Require ≥3 labels for a real subdomain hit.
    if len(parts) < 3:
        return None
    return first


def resolve_tenant_slug(request: Request, settings: Settings) -> str | None:
    """Determine which tenant slug a request is addressing.

    Resolution order:
    1. Explicit `X-Tenant-Slug` header (used by tests and internal tools)
       — outside production only, unless ``TRUST_TENANT_HEADER=true``.
       In production a client could otherwise address any tenant's app
       through the apex host and enumerate slugs, breaking the
       "tenant == host" assumption that host-only cookies and the CSP
       rely on (audit SEC-7).
    2. Subdomain of the `Host` header.
    3. `DEFAULT_TENANT_SLUG` from settings (self-host single-tenant mode).
    """
    if not settings.is_production or settings.trust_tenant_header:
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


class TenantUnavailable(HTTPException):
    """The tenant exists but is deactivated — rendered as a 503 page."""

    def __init__(self, tenant_name: str) -> None:
        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Portal temporarily unavailable",
        )
        self.tenant_name = tenant_name


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

    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    if not tenant.is_active:
        # A known but deactivated portal (billing hard cut, operator
        # suspension) gets a neutral, branded "temporarily unavailable"
        # page instead of a bare 404 — its customers' orders did not
        # vanish, and they deserve to know whom to call (LOGIC-3).
        raise TenantUnavailable(tenant.name)

    # Stash on request.state so downstream code (templates, logging) can
    # use it without re-querying.
    request.state.tenant = tenant
    # The tenant's display zone (UTC -> local), resolved once per request
    # for templates, CSV, PDF and date filters. See ``app.timezones``.
    request.state.tz = tenant_tz(tenant)
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

    Side-effect: binds ``tenant_id`` onto the structlog contextvars so
    every log line emitted during the request inherits it.
    LogContextMiddleware clears the context at request end.
    """
    import structlog

    structlog.contextvars.bind_contextvars(tenant_id=str(tenant.id))
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


async def _stash_subscription_status(
    request: Request,
    db: AsyncSession,
    tenant_id: UUID,
    settings: Settings,
) -> None:
    """One-shot lookup of the tenant's billing subscription status.

    Stash the value on ``request.state`` so the base template can render
    the past_due banner without doing the query itself. No-ops in
    self-hosted mode (``feature_platform=False``) and on any error —
    the banner is a UX hint, not a security control.
    """
    if not settings.feature_platform:
        return
    if hasattr(request.state, "subscription_status"):
        return  # already loaded for this request
    try:
        from datetime import UTC, datetime

        from sqlalchemy import text

        from app.tasks.periodic import access_cutoff

        row = (
            await db.execute(
                text(
                    "SELECT status, trial_ends_at, current_period_end, status_changed_at, "
                    "       canceled_at, updated_at, stripe_subscription_id "
                    "FROM platform_subscriptions WHERE tenant_id = :tid LIMIT 1"
                ),
                {"tid": tenant_id},
            )
        ).one_or_none()
        if row is None:
            request.state.subscription_status = None
            request.state.subscription_info = None
            return
        request.state.subscription_status = row.status
        # Everything the staff banners need: the trial countdown
        # (LOGIC-4) and the date access actually ends (hard cut).
        days_left = None
        if row.status == "trialing" and row.trial_ends_at is not None:
            days_left = max(0, (row.trial_ends_at - datetime.now(UTC)).days)
        request.state.subscription_info = {
            "status": row.status,
            "trial_ends_at": row.trial_ends_at,
            "trial_days_left": days_left,
            "stripe_managed": row.stripe_subscription_id is not None,
            "access_ends_at": access_cutoff(
                row.status,
                current_period_end=row.current_period_end,
                status_changed_at=row.status_changed_at,
                canceled_at=row.canceled_at,
                updated_at=row.updated_at,
            ),
        }
    except Exception:  # pragma: no cover - belt-and-braces
        request.state.subscription_status = None
        request.state.subscription_info = None


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

    import structlog

    if session_data.principal_type == "user":
        result = await db.execute(select(User).where(User.id == principal_uuid))
        user = result.scalar_one_or_none()
        if user is None or not user.is_active:
            return None
        if user.session_version != session_data.session_version:
            return None
        structlog.contextvars.bind_contextvars(
            principal_id=str(user.id),
            principal_type="user",
        )
        await _stash_subscription_status(request, db, tenant.id, settings)
        return Principal.from_user(user)

    if session_data.principal_type == "contact":
        contact_result = await db.execute(
            select(CustomerContact).where(CustomerContact.id == principal_uuid)
        )
        contact = contact_result.scalar_one_or_none()
        if contact is None or not contact.is_active:
            return None
        if contact.session_version != session_data.session_version:
            return None
        # Archived (blocked) customer: every contact is out, including
        # sessions minted via the platform tenant switcher (LOGIC-15).
        customer_active = (
            await db.execute(select(Customer.is_active).where(Customer.id == contact.customer_id))
        ).scalar_one_or_none()
        if not customer_active:
            return None
        structlog.contextvars.bind_contextvars(
            principal_id=str(contact.id),
            principal_type="contact",
        )
        await _stash_subscription_status(request, db, tenant.id, settings)
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
