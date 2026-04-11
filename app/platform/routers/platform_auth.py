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
    identity: Identity | None = Depends(get_current_identity),
) -> HTMLResponse:
    if identity is not None:
        return RedirectResponse(
            url="/platform/select-tenant", status_code=status.HTTP_303_SEE_OTHER
        )
    html = _templates(request).render(
        request,
        "platform/login.html",
        {"error": None, "notice": None, "principal": None},
    )
    return HTMLResponse(html)


@router.post("/platform/login", response_class=HTMLResponse)
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


@router.post("/platform/switch/{tenant_slug}")
async def switch_to_tenant(
    tenant_slug: str,
    request: Request,
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_platform_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Drop a tenant-local session cookie and redirect to the dashboard.

    The endpoint verifies that the caller actually has a membership for
    the target tenant before minting a local session.
    """
    from sqlalchemy import select

    from app.models.tenant import Tenant

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

    if isinstance(target, User):
        principal_type = "user"
        customer_id = None
        full_name = target.full_name
        email_val = target.email
        session_version = target.session_version
        principal_id = target.id
    else:  # CustomerContact
        principal_type = "contact"
        customer_id = target.customer_id
        full_name = target.full_name
        email_val = target.email
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

    response = RedirectResponse(url="/app", status_code=303)
    write_session(
        response,
        settings.app_secret_key,
        session_data,
        secure=settings.is_production,
    )
    # Keep the tenant header so in-process tests & single-domain dev
    # still resolve the tenant after the redirect.
    response.headers["X-Tenant-Slug"] = tenant.slug
    _ = (full_name, email_val)  # silence unused
    return response
