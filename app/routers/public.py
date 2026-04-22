"""Public routes: landing, login, logout, invite accept.

All of these render HTML templates. HTMX fragments are not used here —
auth pages are full page loads.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.deps import get_current_tenant, get_db
from app.i18n import t as _t
from app.models.tenant import Tenant
from app.security.csrf import verify_csrf
from app.security.rate_limit import limit as rate_limit
from app.security.session import (
    SessionData,
    clear_session,
    cookie_mismatches_tenant,
    read_session_for_tenant,
    write_session,
)
from app.services.auth_service import (
    AccountDisabled,
    InvalidCredentials,
    InvalidInvitation,
    LoginResult,
    accept_invitation,
    accept_staff_invite,
    authenticate,
    create_password_reset_token,
    decode_invitation_token,
    decode_password_reset_token,
    decode_staff_invite_token,
    find_principal_by_email,
    reset_password_with_token,
)

router = APIRouter(tags=["public"], dependencies=[Depends(verify_csrf)])

# Invitation links are valid for 7 days.
INVITE_MAX_AGE_SECONDS = 7 * 24 * 3600
# Password reset links are valid for 30 minutes.
PASSWORD_RESET_MAX_AGE_SECONDS = 30 * 60


def _templates(request: Request):
    return request.app.state.templates


def _safe_next_path(candidate: str) -> str:
    """Return ``candidate`` if it is a same-origin path, else ``"/"``.

    Defensive against open-redirect tricks:
    - must start with ``/``
    - must not start with ``//`` (protocol-relative URL)
    - must not contain a backslash (some browsers fold it to ``/``)
    - must not contain ``..`` segments (including URL-encoded ``%2e%2e``
      — round-3 defence-in-depth, Backend P2)
    - must not parse to a non-empty ``netloc`` once normalised
    - may carry a query string and fragment
    """
    from urllib.parse import unquote, urlsplit

    if not candidate or not candidate.startswith("/"):
        return "/"
    if candidate.startswith("//"):
        return "/"
    if "\\" in candidate:
        return "/"
    # Loop-decode to catch double/triple-encoded %2e%2e (%252e%252e).
    decoded = unquote(candidate)
    while unquote(decoded) != decoded:
        decoded = unquote(decoded)
    if ".." in decoded.split("?", 1)[0].split("/"):
        return "/"
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return "/"
    if parts.scheme or parts.netloc:
        return "/"
    return candidate


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
async def landing(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> Response:
    """Root landing.

    * If a tenant resolves AND the visitor is signed in → send them
      straight to the portal (``/app``) — seeing a marketing-style
      "Sign in" CTA when you're already signed in is confusing.
    * If a tenant resolves but no session → show the tenant landing
      with its primary "Sign in" CTA.
    * If no tenant resolves and ``FEATURE_PLATFORM`` is on → render
      the public marketing page on the apex.
    * Otherwise fall back to the tenant landing, which 404s as
      before.
    """
    from app.deps import resolve_tenant_slug

    slug = resolve_tenant_slug(request, settings)
    if slug is None and settings.feature_platform:
        html = _templates(request).render(request, "www/index.html", {"principal": None})
        return HTMLResponse(html)

    from app.deps import get_current_tenant as _get_current_tenant

    tenant = await _get_current_tenant(request, settings)

    # Authenticated for THIS tenant — skip the landing page entirely.
    # A cookie signed for a different tenant must NOT trigger the
    # redirect (otherwise /app 401s → /auth/login → sees cookie →
    # back to /app → loop).
    if read_session_for_tenant(request, settings.app_secret_key, str(tenant.id)) is not None:
        return RedirectResponse(url="/app", status_code=status.HTTP_303_SEE_OTHER)

    response = HTMLResponse(_templates(request).render(request, "index.html", {"tenant": tenant}))
    # Cookie leaked across subdomains (or is otherwise stale) — clear
    # it so the next request starts clean instead of re-triggering the
    # mismatch path.
    if cookie_mismatches_tenant(request, settings.app_secret_key, str(tenant.id)):
        clear_session(response)
    return response


# ------------------------------------------------------------------ login


@router.get("/auth/login", response_class=HTMLResponse)
async def login_form(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
    settings: Settings = Depends(get_settings),
    notice: str | None = None,
    next: str | None = None,
) -> Response:
    # Already-authenticated visitors for THIS tenant should not see the
    # login form. A cookie signed for a different tenant must NOT be
    # honoured here — otherwise a visitor whose browser carries a stale
    # cookie from tenant A hits ERR_TOO_MANY_REDIRECTS on tenant B
    # because /app 401s (tenant mismatch) → /auth/login sees the cookie
    # → bounces to /app → loop.
    if read_session_for_tenant(request, settings.app_secret_key, str(tenant.id)) is not None:
        dest = _safe_next_path(next) if next else "/app"
        if dest in ("/", "/auth/login"):
            dest = "/app"
        return RedirectResponse(url=dest, status_code=status.HTTP_303_SEE_OTHER)

    banner = None
    if notice == "password_reset":
        banner = _t(request, "Password changed. Sign in with your new password.")
    response = HTMLResponse(
        _templates(request).render(
            request,
            "auth/login.html",
            {"tenant": tenant, "error": None, "notice": banner, "next": next or ""},
        )
    )
    # Stamp a Set-Cookie deletion if the inbound cookie was mismatched,
    # so the next request doesn't re-trigger the same path.
    if cookie_mismatches_tenant(request, settings.app_secret_key, str(tenant.id)):
        clear_session(response)
    return response


@router.post("/auth/login", response_class=HTMLResponse)
@rate_limit("20/15 minutes")
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
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
                "error": _t(request, "Your account is disabled."),
                "notice": None,
                "next": next,
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
                "error": _t(request, "Invalid email or password."),
                "notice": None,
                "next": next,
            },
        )
        return HTMLResponse(html, status_code=status.HTTP_401_UNAUTHORIZED)

    dest = _safe_next_path(next) if next else "/app"
    response = RedirectResponse(url=dest, status_code=status.HTTP_303_SEE_OTHER)
    _persist_session(request, response, settings, login)
    return response


# ----------------------------------------------------------------- logout


@router.post("/auth/logout")
async def logout(request: Request) -> Response:
    response = RedirectResponse(url="/auth/login", status_code=status.HTTP_303_SEE_OTHER)
    clear_session(response)
    return response


# ------------------------------------------------------- language switcher


@router.get("/set-lang")
async def set_language(
    request: Request,
    lang: str,
    next: str = "/",
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    """Persist the user's preferred UI locale in a cookie and redirect back.

    The ``next`` query parameter must be a same-origin path starting with
    ``/`` — we strip anything that looks like an open redirect.
    """
    from app.i18n import COOKIE_MAX_AGE, COOKIE_NAME, supported_locale_list

    supported = supported_locale_list(settings.supported_locales)
    chosen = lang.strip().lower() if lang else settings.default_locale
    if chosen not in supported:
        chosen = settings.default_locale

    safe_next = _safe_next_path(next)
    response = RedirectResponse(url=safe_next, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        COOKIE_NAME,
        chosen,
        max_age=COOKIE_MAX_AGE,
        httponly=False,  # harmless JS-readable: users benefit from client-side checks
        samesite="lax",
        secure=settings.is_production,
    )
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
                "error": _t(request, "The invitation is invalid or has expired."),
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
                "error": _t(request, "The invitation belongs to a different tenant."),
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
@rate_limit("10/15 minutes")
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
                "error": _t(request, "Passwords do not match."),
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
                "error": _t(request, "The invitation is invalid or has expired."),
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
                "error": _t(request, "The invitation belongs to a different tenant."),
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


# -------------------------------------------------------- staff invite


@router.get("/invite/staff", response_class=HTMLResponse)
async def staff_invite_form(
    request: Request,
    token: str,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    from sqlalchemy import select

    from app.models.user import User

    try:
        tenant_id, user_id = decode_staff_invite_token(
            settings.app_secret_key, token, max_age_seconds=INVITE_MAX_AGE_SECONDS
        )
    except InvalidInvitation:
        html = _templates(request).render(
            request,
            "auth/staff_invite_accept.html",
            {
                "tenant": tenant,
                "token": token,
                "user": None,
                "error": _t(request, "The invitation is invalid or has expired."),
                "notice": None,
            },
        )
        return HTMLResponse(html, status_code=400)

    if tenant_id != tenant.id:
        raise InvalidInvitation("tenant mismatch")

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()

    html = _templates(request).render(
        request,
        "auth/staff_invite_accept.html",
        {
            "tenant": tenant,
            "token": token,
            "user": user,
            "error": None,
            "notice": None,
        },
    )
    return HTMLResponse(html)


@router.post("/invite/staff", response_class=HTMLResponse)
@rate_limit("10/15 minutes")
async def staff_invite_submit(
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
            "auth/staff_invite_accept.html",
            {
                "tenant": tenant,
                "token": token,
                "user": None,
                "error": _t(request, "Passwords do not match."),
                "notice": None,
            },
        )
        return HTMLResponse(html, status_code=400)

    try:
        tenant_id, user_id = decode_staff_invite_token(
            settings.app_secret_key, token, max_age_seconds=INVITE_MAX_AGE_SECONDS
        )
    except InvalidInvitation as exc:
        html = _templates(request).render(
            request,
            "auth/staff_invite_accept.html",
            {
                "tenant": tenant,
                "token": token,
                "user": None,
                "error": str(exc),
                "notice": None,
            },
        )
        return HTMLResponse(html, status_code=400)

    if tenant_id != tenant.id:
        return HTMLResponse("Tenant mismatch", status_code=400)

    try:
        user = await accept_staff_invite(
            db, tenant_id=tenant_id, user_id=user_id, password=password
        )
    except InvalidInvitation as exc:
        html = _templates(request).render(
            request,
            "auth/staff_invite_accept.html",
            {
                "tenant": tenant,
                "token": token,
                "user": None,
                "error": str(exc),
                "notice": None,
            },
        )
        return HTMLResponse(html, status_code=400)

    # Auto-login the newly activated staff user.
    login = LoginResult(
        principal_type="user",
        principal_id=user.id,
        tenant_id=user.tenant_id,
        customer_id=None,
        full_name=user.full_name,
        email=user.email,
        session_version=user.session_version,
    )
    response = _login_redirect(request)
    _persist_session(request, response, settings, login)
    return response


# ------------------------------------------------------ password reset


@router.get("/auth/password-reset", response_class=HTMLResponse)
async def password_reset_request_form(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
    settings: Settings = Depends(get_settings),
) -> Response:
    # Users already signed in to THIS tenant change their password via
    # the profile page — the public reset flow exists only for "I
    # forgot my password" visitors. Cross-tenant stale cookies are
    # treated as "no session" here for the same reason as /auth/login.
    if read_session_for_tenant(request, settings.app_secret_key, str(tenant.id)) is not None:
        return RedirectResponse(url="/app/admin/profile", status_code=status.HTTP_303_SEE_OTHER)
    response = HTMLResponse(
        _templates(request).render(
            request,
            "auth/password_reset_request.html",
            {"tenant": tenant, "error": None, "notice": None},
        )
    )
    if cookie_mismatches_tenant(request, settings.app_secret_key, str(tenant.id)):
        clear_session(response)
    return response


@router.post("/auth/password-reset", response_class=HTMLResponse)
@rate_limit("5/15 minutes")
async def password_reset_request_submit(
    request: Request,
    background_tasks: BackgroundTasks,
    email: str = Form(...),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Always respond with a generic success page to avoid email enumeration.

    The actual e-mail is only sent if the address matches an active
    principal. Otherwise we just log and return success.
    """
    match = await find_principal_by_email(db, email)
    if match is not None:
        principal_type, row = match
        reset_token = create_password_reset_token(
            settings.app_secret_key,
            tenant_id=tenant.id,
            principal_type=principal_type,
            principal_id=row.id,
        )
        from app.urls import tenant_base_url

        reset_url = (
            f"{tenant_base_url(settings, tenant)}/auth/password-reset/confirm?token={reset_token}"
        )
        sender = request.app.state.email_sender
        from app.services.customer_service import get_customer
        from app.services.locale_service import resolve_email_locale
        from app.tasks.email_tasks import send_password_reset

        # For a customer contact, also look at the customer-level default
        # (lets a tenant admin set an EN default for a US customer without
        # having to flip each contact's row).
        customer = None
        if principal_type == "contact":
            customer = await get_customer(db, row.customer_id)
        locale = resolve_email_locale(
            recipient=row, customer=customer, tenant=tenant, settings=settings
        )

        background_tasks.add_task(
            send_password_reset,
            sender,
            to=row.email,
            tenant_name=tenant.name,
            full_name=row.full_name,
            reset_url=reset_url,
            locale=locale,
        )

    html = _templates(request).render(
        request,
        "auth/password_reset_request.html",
        {
            "tenant": tenant,
            "error": None,
            "notice": "Pokud adresa existuje, odeslali jsme odkaz na obnovu hesla.",
        },
    )
    return HTMLResponse(html)


@router.get("/auth/password-reset/confirm", response_class=HTMLResponse)
async def password_reset_confirm_form(
    request: Request,
    token: str,
    tenant: Tenant = Depends(get_current_tenant),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    try:
        tenant_id, _pt, _pid = decode_password_reset_token(
            settings.app_secret_key,
            token,
            max_age_seconds=PASSWORD_RESET_MAX_AGE_SECONDS,
        )
    except InvalidInvitation:
        html = _templates(request).render(
            request,
            "auth/password_reset_confirm.html",
            {
                "tenant": tenant,
                "token": token,
                "error": _t(request, "The link is invalid or has expired."),
                "notice": None,
            },
        )
        return HTMLResponse(html, status_code=400)

    if tenant_id != tenant.id:
        return HTMLResponse("Tenant mismatch", status_code=400)

    html = _templates(request).render(
        request,
        "auth/password_reset_confirm.html",
        {"tenant": tenant, "token": token, "error": None, "notice": None},
    )
    return HTMLResponse(html)


@router.post("/auth/password-reset/confirm", response_class=HTMLResponse)
@rate_limit("10/15 minutes")
async def password_reset_confirm_submit(
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
            "auth/password_reset_confirm.html",
            {
                "tenant": tenant,
                "token": token,
                "error": _t(request, "Passwords do not match."),
                "notice": None,
            },
        )
        return HTMLResponse(html, status_code=400)

    try:
        tenant_id, principal_type, principal_id = decode_password_reset_token(
            settings.app_secret_key,
            token,
            max_age_seconds=PASSWORD_RESET_MAX_AGE_SECONDS,
        )
    except InvalidInvitation as exc:
        html = _templates(request).render(
            request,
            "auth/password_reset_confirm.html",
            {
                "tenant": tenant,
                "token": token,
                "error": str(exc),
                "notice": None,
            },
        )
        return HTMLResponse(html, status_code=400)

    if tenant_id != tenant.id:
        return HTMLResponse("Tenant mismatch", status_code=400)

    try:
        await reset_password_with_token(
            db,
            tenant_id=tenant_id,
            principal_type=principal_type,
            principal_id=principal_id,
            new_password=password,
        )
    except InvalidInvitation as exc:
        html = _templates(request).render(
            request,
            "auth/password_reset_confirm.html",
            {
                "tenant": tenant,
                "token": token,
                "error": str(exc),
                "notice": None,
            },
        )
        return HTMLResponse(html, status_code=400)

    return RedirectResponse(
        url="/auth/login?notice=password_reset",
        status_code=303,
    )
