"""Platform-level auth routes: login, logout, tenant switcher."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models.customer import CustomerContact
from app.models.user import User
from app.platform.deps import get_current_identity, get_platform_db, require_identity
from app.platform.models import Identity
from app.platform.service import (
    AccountDisabled,
    InvalidCredentials,
    authenticate_identity,
    list_customer_name_for_contact,
    list_memberships_for_identity,
    resolve_membership_targets,
)
from app.platform.session import (
    PlatformSession,
    clear_platform_session,
    write_platform_session,
)
from app.security.csrf import verify_csrf
from app.security.rate_limit import limit as rate_limit
from app.security.session import SessionData, write_session

router = APIRouter(tags=["platform-auth"], dependencies=[Depends(verify_csrf)])


def _templates(request: Request):
    return request.app.state.templates


def _cookie_domain(settings: Settings) -> str | None:
    return settings.platform_cookie_domain or None


# ---------------------------------------------------------------- login


@router.get("/platform/login", response_class=HTMLResponse)
async def platform_login_form(
    request: Request,
    notice: str | None = None,
    identity: Identity | None = Depends(get_current_identity),
) -> HTMLResponse:
    if identity is not None:
        return RedirectResponse(
            url="/platform/select-tenant", status_code=status.HTTP_303_SEE_OTHER
        )
    banner = None
    if notice == "password_reset":
        banner = "Heslo bylo úspěšně změněno. Přihlaste se novým heslem."
    html = _templates(request).render(
        request,
        "platform/login.html",
        {"error": None, "notice": banner, "principal": None},
    )
    return HTMLResponse(html)


@router.post("/platform/login", response_class=HTMLResponse)
@rate_limit("20/15 minutes")
async def platform_login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_platform_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    try:
        identity = await authenticate_identity(db, email, password)
    except (InvalidCredentials, AccountDisabled) as exc:
        message = (
            "Účet je deaktivován."
            if isinstance(exc, AccountDisabled)
            else "Neplatný e-mail nebo heslo."
        )
        html = _templates(request).render(
            request,
            "platform/login.html",
            {"error": message, "notice": None, "principal": None},
        )
        return HTMLResponse(html, status_code=401)

    await db.commit()

    response = RedirectResponse(
        url="/platform/select-tenant", status_code=status.HTTP_303_SEE_OTHER
    )
    write_platform_session(
        response,
        settings.app_secret_key,
        PlatformSession(
            identity_id=str(identity.id),
            is_platform_admin=identity.is_platform_admin,
        ),
        domain=_cookie_domain(settings),
        secure=settings.is_production,
    )
    return response


@router.post("/platform/logout")
async def platform_logout(
    settings: Settings = Depends(get_settings),
) -> Response:
    response = RedirectResponse(url="/platform/login", status_code=status.HTTP_303_SEE_OTHER)
    clear_platform_session(response, domain=_cookie_domain(settings))
    return response


# -------------------------------------------------- password reset

PLATFORM_RESET_MAX_AGE = 30 * 60  # 30 minutes


@router.get("/platform/password-reset", response_class=HTMLResponse)
async def platform_password_reset_form(
    request: Request,
    identity: Identity | None = Depends(get_current_identity),
) -> Response:
    # Signed-in identities don't need the forgot-password flow. Send
    # them to the tenant selector (where they can sign out if they
    # hit this page by mistake).
    if identity is not None:
        return RedirectResponse(
            url="/platform/select-tenant", status_code=status.HTTP_303_SEE_OTHER
        )
    html = _templates(request).render(
        request,
        "platform/password_reset_request.html",
        {"error": None, "notice": None, "principal": None},
    )
    return HTMLResponse(html)


@router.post("/platform/password-reset", response_class=HTMLResponse)
@rate_limit("5/15 minutes")
async def platform_password_reset_submit(
    request: Request,
    email: str = Form(...),
    db: AsyncSession = Depends(get_platform_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    from app.platform.service import (
        create_platform_password_reset_token,
        find_identity_by_email,
    )
    from app.tasks.email_tasks import send_password_reset

    identity = await find_identity_by_email(db, email)
    if identity is not None and identity.is_active:
        reset_token = create_platform_password_reset_token(settings.app_secret_key, identity.id)
        reset_url = f"{settings.app_base_url}/platform/password-reset/confirm?token={reset_token}"
        sender = request.app.state.email_sender
        # Platform identities aren't scoped to a tenant, so there's no
        # tenant default to inherit — use whatever the browser is
        # currently showing the user (the language switcher on the
        # public page sets this cookie).
        locale = getattr(request.state, "locale", settings.default_locale)
        send_password_reset(
            sender,
            to=identity.email,
            tenant_name="Assoluto",
            full_name=identity.full_name,
            reset_url=reset_url,
            locale=locale,
        )

    html = _templates(request).render(
        request,
        "platform/password_reset_request.html",
        {
            "error": None,
            "notice": "Pokud adresa existuje, odeslali jsme odkaz na obnovu hesla.",
            "principal": None,
        },
    )
    return HTMLResponse(html)


@router.get("/platform/password-reset/confirm", response_class=HTMLResponse)
async def platform_password_reset_confirm_form(
    request: Request,
    token: str,
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    from app.platform.service import decode_platform_password_reset_token

    try:
        decode_platform_password_reset_token(settings.app_secret_key, token, PLATFORM_RESET_MAX_AGE)
    except Exception:
        html = _templates(request).render(
            request,
            "platform/password_reset_confirm.html",
            {
                "token": token,
                "error": "Odkaz je neplatný nebo vypršel.",
                "notice": None,
                "principal": None,
            },
        )
        return HTMLResponse(html, status_code=400)

    html = _templates(request).render(
        request,
        "platform/password_reset_confirm.html",
        {"token": token, "error": None, "notice": None, "principal": None},
    )
    return HTMLResponse(html)


@router.post("/platform/password-reset/confirm", response_class=HTMLResponse)
@rate_limit("10/15 minutes")
async def platform_password_reset_confirm_submit(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: AsyncSession = Depends(get_platform_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    if password != password_confirm:
        html = _templates(request).render(
            request,
            "platform/password_reset_confirm.html",
            {
                "token": token,
                "error": "Hesla se neshodují.",
                "notice": None,
                "principal": None,
            },
        )
        return HTMLResponse(html, status_code=400)

    from app.platform.service import (
        decode_platform_password_reset_token,
        reset_platform_password,
    )

    try:
        identity_id = decode_platform_password_reset_token(
            settings.app_secret_key, token, PLATFORM_RESET_MAX_AGE
        )
    except Exception:
        html = _templates(request).render(
            request,
            "platform/password_reset_confirm.html",
            {
                "token": token,
                "error": "Odkaz je neplatný nebo vypršel.",
                "notice": None,
                "principal": None,
            },
        )
        return HTMLResponse(html, status_code=400)

    try:
        await reset_platform_password(db, identity_id, password)
        await db.commit()
    except Exception:
        html = _templates(request).render(
            request,
            "platform/password_reset_confirm.html",
            {
                "token": token,
                "error": "Nepodařilo se nastavit nové heslo.",
                "notice": None,
                "principal": None,
            },
        )
        return HTMLResponse(html, status_code=400)

    return RedirectResponse(
        url="/platform/login?notice=password_reset",
        status_code=303,
    )


# ------------------------------------------------------- tenant picker


@router.get("/platform/select-tenant", response_class=HTMLResponse)
async def select_tenant(
    request: Request,
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_platform_db),
) -> HTMLResponse:
    memberships = await list_memberships_for_identity(db, identity_id=identity.id)

    resolved = []
    for m in memberships:
        tenant, target = await resolve_membership_targets(db, membership=m)
        if tenant is None or not tenant.is_active:
            continue
        customer_name: str | None = None
        if isinstance(target, CustomerContact):
            customer_name = await list_customer_name_for_contact(db, target.id)
        resolved.append(
            {
                "membership_id": str(m.id),
                "tenant_slug": tenant.slug,
                "tenant_name": tenant.name,
                "kind": "staff" if isinstance(target, User) else "contact",
                "customer_name": customer_name,
                "access_type": m.access_type,
            }
        )

    html = _templates(request).render(
        request,
        "platform/select_tenant.html",
        {
            "identity": identity,
            "memberships": resolved,
            "error": None,
            "notice": None,
            "principal": None,
        },
    )
    return HTMLResponse(html)


# ------------------------------------------------- switch into a tenant


def _target_tenant_base_url(settings: Settings, tenant_slug: str) -> str | None:
    """Build an absolute URL for the target tenant subdomain, or None if
    the deployment runs in single-host mode (no platform cookie domain).

    Used by ``switch_to_tenant`` to hand off the session mint to the
    tenant's subdomain. Single-host installs (local dev) stay on the
    current host and let the existing in-place cookie work.
    """
    cookie_domain = (settings.platform_cookie_domain or "").strip()
    if not cookie_domain or cookie_domain == ".":
        return None
    parent = cookie_domain.lstrip(".")
    if not parent or "." not in parent:
        return None
    return f"https://{tenant_slug}.{parent}"


@router.post("/platform/switch/{tenant_slug}")
async def switch_to_tenant(
    tenant_slug: str,
    request: Request,
    next: str = Form(""),
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_platform_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Verify membership and hand off to the tenant subdomain.

    Session cookies are per-subdomain by design (tenant isolation), so
    minting a session on the platform apex and then redirecting to
    ``tenant.example.com/app`` would lose it. Instead this endpoint:

    1. Verifies membership + target account is active.
    2. Signs a one-shot handoff token with (identity_id, membership_id).
    3. 303-redirects to ``https://{slug}.<apex>/platform/complete-switch?token=…``.

    The target subdomain's ``complete_switch`` endpoint re-verifies the
    token against the still-valid platform session cookie (which rides
    along cross-subdomain thanks to ``platform_cookie_domain`` being the
    apex) and mints the tenant session cookie in the right scope.

    In single-host dev (no ``platform_cookie_domain``) we fall back to
    the historical same-host behaviour — the cookie lands on localhost
    just like the app expects.
    """
    from sqlalchemy import select

    from app.models.tenant import Tenant
    from app.routers.public import _safe_next_path
    from app.security.tokens import TokenPurpose, create_token

    tenant = (
        await db.execute(select(Tenant).where(Tenant.slug == tenant_slug))
    ).scalar_one_or_none()
    if tenant is None or not tenant.is_active:
        raise HTTPException(status_code=404, detail="Tenant not found")

    memberships = await list_memberships_for_identity(db, identity_id=identity.id)
    selected = None
    for m in memberships:
        if m.tenant_id == tenant.id:
            selected = m
            break
    if selected is None:
        raise HTTPException(status_code=403, detail="No membership for this tenant")

    _, target = await resolve_membership_targets(db, membership=selected)
    if target is None:
        raise HTTPException(status_code=404, detail="Membership target missing")
    # Round-3 audit Backend P2: refuse to mint a session for a
    # deactivated User / CustomerContact even when the membership
    # row still exists. Prevents a zombie cookie from being issued.
    if not getattr(target, "is_active", True):
        raise HTTPException(status_code=403, detail="Target account is deactivated")

    next_path = _safe_next_path(next) if next else "/app"
    if next_path == "/":
        next_path = "/app"

    target_base = _target_tenant_base_url(settings, tenant_slug)
    if target_base is not None:
        # Cross-subdomain hand-off (normal hosted deployment).
        token = create_token(
            settings.app_secret_key,
            TokenPurpose.PLATFORM_TENANT_HANDOFF,
            {
                "iid": str(identity.id),
                "mid": str(selected.id),
                "tid": str(tenant.id),
                "next": next_path,
            },
        )
        handoff_url = f"{target_base}/platform/complete-switch?token={token}"
        return RedirectResponse(url=handoff_url, status_code=303)

    # Single-host fallback (dev / self-host without subdomain routing).
    # Mint the session cookie on the current host and redirect in-place,
    # matching the pre-handoff behaviour.
    if isinstance(target, User):
        principal_type = "user"
        customer_id = None
        session_version = target.session_version
        principal_id = target.id
    else:  # CustomerContact
        principal_type = "contact"
        customer_id = target.customer_id
        session_version = target.session_version
        principal_id = target.id

    session_data = SessionData(
        principal_type=principal_type,  # type: ignore[arg-type]
        principal_id=str(principal_id),
        tenant_id=str(tenant.id),
        customer_id=str(customer_id) if customer_id else None,
        mfa_passed=False,
        session_version=session_version,
    )

    response = RedirectResponse(url=next_path, status_code=303)
    write_session(
        response,
        settings.app_secret_key,
        session_data,
        secure=settings.is_production,
    )
    # Keep the tenant header so in-process tests still resolve the tenant.
    response.headers["X-Tenant-Slug"] = tenant.slug
    return response


@router.get("/platform/complete-switch")
async def complete_switch(
    request: Request,
    token: str,
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_platform_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Consume the one-shot handoff token on the target tenant subdomain.

    Sanity rules:
    - The platform session must still be valid and match the token's
      identity claim — a stolen token alone cannot mint a tenant session.
    - Membership is re-checked by id (we don't trust the token to name a
      tenant the identity doesn't actually belong to).
    - Token max-age is 60 s; signatures expiring beyond that are rejected.
    """
    from sqlalchemy import select

    from app.models.tenant import Tenant
    from app.routers.public import _safe_next_path
    from app.security.tokens import (
        ExpiredToken,
        InvalidToken,
        TokenPurpose,
        verify_token,
    )

    try:
        payload = verify_token(
            settings.app_secret_key,
            TokenPurpose.PLATFORM_TENANT_HANDOFF,
            token,
            max_age_seconds=60,
        )
    except ExpiredToken:
        raise HTTPException(status_code=400, detail="Handoff token expired") from None
    except InvalidToken:
        raise HTTPException(status_code=400, detail="Invalid handoff token") from None

    # The identity that signed the handoff must match the cookie-holder.
    if str(identity.id) != str(payload.get("iid")):
        raise HTTPException(status_code=403, detail="Identity mismatch")

    tenant_id = payload.get("tid")
    membership_id = payload.get("mid")
    next_path = _safe_next_path(payload.get("next") or "/app")
    if next_path == "/":
        next_path = "/app"

    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if tenant is None or not tenant.is_active:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # Re-fetch the membership server-side — never trust the token alone.
    memberships = await list_memberships_for_identity(db, identity_id=identity.id)
    selected = None
    for m in memberships:
        if str(m.id) == str(membership_id) and m.tenant_id == tenant.id:
            selected = m
            break
    if selected is None:
        raise HTTPException(status_code=403, detail="No membership for this tenant")

    _, target = await resolve_membership_targets(db, membership=selected)
    if target is None:
        raise HTTPException(status_code=404, detail="Membership target missing")
    if not getattr(target, "is_active", True):
        raise HTTPException(status_code=403, detail="Target account is deactivated")

    if isinstance(target, User):
        principal_type = "user"
        customer_id = None
        session_version = target.session_version
        principal_id = target.id
    else:
        principal_type = "contact"
        customer_id = target.customer_id
        session_version = target.session_version
        principal_id = target.id

    session_data = SessionData(
        principal_type=principal_type,  # type: ignore[arg-type]
        principal_id=str(principal_id),
        tenant_id=str(tenant.id),
        customer_id=str(customer_id) if customer_id else None,
        mfa_passed=False,
        session_version=session_version,
    )

    # When a platform admin enters a tenant via support-access, drop an
    # audit event into the TENANT's log so tenant admins can see exactly
    # when the operator was inside their portal. Without this, subsequent
    # actions attribute to the support User row and look indistinguishable
    # from a regular tenant-admin session.
    from app.platform.models import MEMBERSHIP_ACCESS_SUPPORT

    if selected.access_type == MEMBERSHIP_ACCESS_SUPPORT and isinstance(target, User):
        from app.db.session import get_sessionmaker
        from app.deps import set_tenant_context
        from app.services.audit_service import ActorInfo, record

        # A fresh RLS-scoped session so the audit row is written against
        # the target tenant, not the platform-owner db we're on here.
        sm = get_sessionmaker()
        async with sm() as tenant_session, tenant_session.begin():
            await set_tenant_context(tenant_session, str(tenant.id))
            await record(
                tenant_session,
                action="platform.support_access_session_started",
                entity_type="tenant",
                entity_id=tenant.id,
                entity_label=tenant.name,
                actor=ActorInfo(
                    type="user",
                    id=target.id,
                    label=f"{target.full_name} <{target.email}> (support)",
                ),
                after={
                    "platform_identity_id": str(identity.id),
                    "platform_identity_email": identity.email,
                    "membership_id": str(selected.id),
                    "granted_at": selected.created_at.isoformat()
                    if selected.created_at
                    else None,
                },
                tenant_id=tenant.id,
            )

    response = RedirectResponse(url=next_path, status_code=303)
    write_session(
        response,
        settings.app_secret_key,
        session_data,
        secure=settings.is_production,
    )
    return response
