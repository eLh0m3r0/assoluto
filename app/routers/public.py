"""Public routes: landing, login, logout, invite accept.

All of these render HTML templates. HTMX fragments are not used here —
auth pages are full page loads.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.deps import get_current_tenant, get_db
from app.models.tenant import Tenant
from app.security.session import SessionData, clear_session, write_session
from app.services.auth_service import (
    AccountDisabled,
    InvalidCredentials,
    InvalidInvitation,
    LoginResult,
    accept_invitation,
    authenticate,
    decode_invitation_token,
)

router = APIRouter(tags=["public"])

# Invitation links are valid for 7 days.
INVITE_MAX_AGE_SECONDS = 7 * 24 * 3600


def _templates(request: Request):
    return request.app.state.templates


def _login_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse(url="/app", status_code=status.HTTP_303_SEE_OTHER)


def _persist_session(
    request: Request,
    response: Response,
    settings: Settings,
    login: LoginResult,
) -> None:
    session_data = SessionData(
        principal_type=login.principal_type,  # type: ignore[arg-type]
        principal_id=str(login.principal_id),
        tenant_id=str(login.tenant_id),
        customer_id=str(login.customer_id) if login.customer_id else None,
        mfa_passed=False,
        session_version=login.session_version,
    )
    write_session(
        response,
        settings.app_secret_key,
        session_data,
        secure=settings.is_production,
    )


# ---------------------------------------------------------------- landing


@router.get("/", response_class=HTMLResponse)
async def landing(request: Request, tenant: Tenant = Depends(get_current_tenant)) -> HTMLResponse:
    html = _templates(request).render(request, "index.html", {"tenant": tenant})
    return HTMLResponse(html)


# ------------------------------------------------------------------ login


@router.get("/auth/login", response_class=HTMLResponse)
async def login_form(
    request: Request, tenant: Tenant = Depends(get_current_tenant)
) -> HTMLResponse:
    html = _templates(request).render(
        request, "auth/login.html", {"tenant": tenant, "error": None, "notice": None}
    )
    return HTMLResponse(html)


@router.post("/auth/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    try:
        login = await authenticate(db, email, password)
    except AccountDisabled:
        html = _templates(request).render(
            request,
            "auth/login.html",
            {
                "tenant": tenant,
                "email": email,
                "error": "Účet je deaktivován.",
                "notice": None,
            },
        )
        return HTMLResponse(html, status_code=status.HTTP_403_FORBIDDEN)
    except InvalidCredentials:
        html = _templates(request).render(
            request,
            "auth/login.html",
            {
                "tenant": tenant,
                "email": email,
                "error": "Neplatný e-mail nebo heslo.",
                "notice": None,
            },
        )
        return HTMLResponse(html, status_code=status.HTTP_401_UNAUTHORIZED)

    response = _login_redirect(request)
    _persist_session(request, response, settings, login)
    return response


# ----------------------------------------------------------------- logout


@router.post("/auth/logout")
async def logout(request: Request) -> Response:
    response = RedirectResponse(url="/auth/login", status_code=status.HTTP_303_SEE_OTHER)
    clear_session(response)
    return response


# ----------------------------------------------------------- invite accept


@router.get("/invite/accept", response_class=HTMLResponse)
async def invite_accept_form(
    request: Request,
    token: str,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    try:
        tenant_id, contact_id = decode_invitation_token(
            settings.app_secret_key, token, max_age_seconds=INVITE_MAX_AGE_SECONDS
        )
    except InvalidInvitation:
        html = _templates(request).render(
            request,
            "auth/invite_accept.html",
            {
                "tenant": tenant,
                "token": token,
                "contact": None,
                "error": "Pozvánka je neplatná nebo vypršela.",
                "notice": None,
            },
        )
        return HTMLResponse(html, status_code=status.HTTP_400_BAD_REQUEST)

    if tenant_id != tenant.id:
        html = _templates(request).render(
            request,
            "auth/invite_accept.html",
            {
                "tenant": tenant,
                "token": token,
                "contact": None,
                "error": "Pozvánka patří k jinému tenantovi.",
                "notice": None,
            },
        )
        return HTMLResponse(html, status_code=status.HTTP_400_BAD_REQUEST)

    # Load the contact so we can show their name on the form.
    from sqlalchemy import select

    from app.models.customer import CustomerContact

    contact = (
        await db.execute(select(CustomerContact).where(CustomerContact.id == contact_id))
    ).scalar_one_or_none()

    if contact is None:
        html = _templates(request).render(
            request,
            "auth/invite_accept.html",
            {
                "tenant": tenant,
                "token": token,
                "contact": None,
                "error": "Kontakt neexistuje.",
                "notice": None,
            },
        )
        return HTMLResponse(html, status_code=status.HTTP_404_NOT_FOUND)

    html = _templates(request).render(
        request,
        "auth/invite_accept.html",
        {
            "tenant": tenant,
            "token": token,
            "contact": contact,
            "error": None,
            "notice": None,
        },
    )
    return HTMLResponse(html)


@router.post("/invite/accept", response_class=HTMLResponse)
async def invite_accept_submit(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    if password != password_confirm:
        html = _templates(request).render(
            request,
            "auth/invite_accept.html",
            {
                "tenant": tenant,
                "token": token,
                "contact": None,
                "error": "Hesla se neshodují.",
                "notice": None,
            },
        )
        return HTMLResponse(html, status_code=status.HTTP_400_BAD_REQUEST)

    try:
        tenant_id, contact_id = decode_invitation_token(
            settings.app_secret_key,
            token,
            max_age_seconds=INVITE_MAX_AGE_SECONDS,
        )
    except InvalidInvitation:
        html = _templates(request).render(
            request,
            "auth/invite_accept.html",
            {
                "tenant": tenant,
                "token": token,
                "contact": None,
                "error": "Pozvánka je neplatná nebo vypršela.",
                "notice": None,
            },
        )
        return HTMLResponse(html, status_code=status.HTTP_400_BAD_REQUEST)

    if tenant_id != tenant.id:
        html = _templates(request).render(
            request,
            "auth/invite_accept.html",
            {
                "tenant": tenant,
                "token": token,
                "contact": None,
                "error": "Pozvánka patří k jinému tenantovi.",
                "notice": None,
            },
        )
        return HTMLResponse(html, status_code=status.HTTP_400_BAD_REQUEST)

    try:
        contact = await accept_invitation(
            db,
            tenant_id=tenant_id,
            contact_id=contact_id,
            password=password,
        )
    except InvalidInvitation as exc:
        html = _templates(request).render(
            request,
            "auth/invite_accept.html",
            {
                "tenant": tenant,
                "token": token,
                "contact": None,
                "error": str(exc),
                "notice": None,
            },
        )
        return HTMLResponse(html, status_code=status.HTTP_400_BAD_REQUEST)

    # Auto-login after successful acceptance.
    login = LoginResult(
        principal_type="contact",
        principal_id=contact.id,
        tenant_id=contact.tenant_id,
        customer_id=contact.customer_id,
        full_name=contact.full_name,
        email=contact.email,
        session_version=contact.session_version,
    )
    response = _login_redirect(request)
    _persist_session(request, response, settings, login)
    return response
