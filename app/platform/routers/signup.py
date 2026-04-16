"""Self-signup, email verification, and onboarding routes.

Lives under the platform package because these flows only make sense
when the hosted SaaS layer is turned on (``FEATURE_PLATFORM=true``).
Core self-hosted builds never mount this router.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.platform.deps import get_current_identity, get_platform_db, require_identity
from app.platform.models import Identity
from app.platform.service import (
    DuplicateIdentityEmail,
    DuplicateTenantSlug,
    PlatformError,
    mark_email_verified,
    signup_tenant,
)
from app.platform.session import PlatformSession, write_platform_session
from app.platform.validation import SignupValidationError, parse_signup_form
from app.security.csrf import verify_csrf
from app.security.rate_limit import limit as rate_limit
from app.security.tokens import (
    ExpiredToken,
    InvalidToken,
    TokenPurpose,
    create_token,
    verify_token,
)
from app.tasks.email_tasks import send_email_verification

router = APIRouter(tags=["platform-signup"], dependencies=[Depends(verify_csrf)])

# Verification tokens live for 24 h. Long enough to survive an overnight
# mail-delay, short enough to limit harm if a mailbox is compromised.
VERIFY_TOKEN_MAX_AGE_SECONDS = 24 * 3600


def _templates(request: Request):
    return request.app.state.templates


def _cookie_domain(settings: Settings) -> str | None:
    return settings.platform_cookie_domain or None


# ---------------------------------------------------------------- signup


@router.get("/platform/signup", response_class=HTMLResponse)
async def signup_form(
    request: Request,
    identity: Identity | None = Depends(get_current_identity),
) -> HTMLResponse:
    """Show the registration form (or bounce to tenant picker if logged in)."""
    if identity is not None:
        return RedirectResponse(
            url="/platform/select-tenant", status_code=status.HTTP_303_SEE_OTHER
        )
    html = _templates(request).render(
        request,
        "platform/signup.html",
        {"errors": {}, "form": {}, "principal": None},
    )
    return HTMLResponse(html)


def _safe_plan_code(plan: str) -> str:
    """Allow only the public plan codes through to the verify-sent redirect."""
    allowed = {"starter", "pro"}
    return plan if plan in allowed else ""


@router.post("/platform/signup", response_class=HTMLResponse)
@rate_limit("10/15 minutes")
async def signup_submit(
    request: Request,
    background_tasks: BackgroundTasks,
    company_name: str = Form(...),
    slug: str = Form(""),
    owner_email: str = Form(...),
    owner_full_name: str = Form(""),
    password: str = Form(...),
    terms_accepted: str = Form(""),
    plan: str = Form(""),
    db: AsyncSession = Depends(get_platform_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    form_raw = {
        "company_name": company_name,
        "slug": slug,
        "owner_email": owner_email,
        "owner_full_name": owner_full_name,
        # Intentionally not echoing the password back.
    }

    # 1) Validate shape
    try:
        form = parse_signup_form(
            company_name=company_name,
            slug=slug,
            owner_email=owner_email,
            owner_full_name=owner_full_name,
            password=password,
            terms_accepted=bool(terms_accepted),
        )
    except SignupValidationError as exc:
        html = _templates(request).render(
            request,
            "platform/signup.html",
            {
                "errors": {exc.field: exc.message},
                "form": form_raw,
                "principal": None,
            },
        )
        return HTMLResponse(html, status_code=400)

    # 2) Provision tenant + owner user + Identity
    try:
        tenant, _owner, identity = await signup_tenant(
            db,
            company_name=form.company_name,
            slug=form.slug,
            owner_email=form.owner_email,
            owner_full_name=form.owner_full_name,
            owner_password=form.password,
        )
    except DuplicateTenantSlug:
        html = _templates(request).render(
            request,
            "platform/signup.html",
            {
                "errors": {"slug": "Tato subdoména je již obsazená."},
                "form": form_raw,
                "principal": None,
            },
        )
        return HTMLResponse(html, status_code=400)
    except DuplicateIdentityEmail:
        html = _templates(request).render(
            request,
            "platform/signup.html",
            {
                "errors": {
                    "owner_email": "Účet s tímto e-mailem již existuje. Použijte přihlášení."
                },
                "form": form_raw,
                "principal": None,
            },
        )
        return HTMLResponse(html, status_code=400)
    except PlatformError as exc:
        html = _templates(request).render(
            request,
            "platform/signup.html",
            {
                "errors": {"company_name": f"Chyba: {exc}"},
                "form": form_raw,
                "principal": None,
            },
        )
        return HTMLResponse(html, status_code=400)

    # Preserve the plan the user clicked on the pricing page so verify-sent /
    # verify-email can surface a "Finish setup" CTA that leads into checkout.
    selected_plan = _safe_plan_code(plan)
    if selected_plan:
        tenant_settings = dict(tenant.settings or {})
        tenant_settings["selected_plan"] = selected_plan
        tenant.settings = tenant_settings
        await db.flush()

    # 3) Commit BEFORE scheduling the email task (BackgroundTasks run before
    # the request-scoped session commit; see CLAUDE.md for the pattern).
    await db.commit()

    verify_url = _build_verify_url(settings, identity.id)
    sender = request.app.state.email_sender
    background_tasks.add_task(
        send_email_verification,
        sender,
        to=identity.email,
        full_name=identity.full_name,
        company_name=tenant.name,
        verify_url=verify_url,
    )

    # 4) Log the new user straight in via the platform cookie — no need
    # to make them type their password again.
    response = RedirectResponse(url="/platform/verify-sent", status_code=status.HTTP_303_SEE_OTHER)
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


def _build_verify_url(settings: Settings, identity_id: UUID) -> str:
    token = create_token(
        settings.app_secret_key,
        TokenPurpose.EMAIL_VERIFY,
        {"identity_id": str(identity_id)},
    )
    base = settings.app_base_url.rstrip("/")
    return f"{base}/platform/verify-email?token={token}"


# ------------------------------------------------------- "check your inbox"


@router.get("/platform/verify-sent", response_class=HTMLResponse)
async def verify_sent(
    request: Request,
    identity: Identity = Depends(require_identity),
) -> HTMLResponse:
    html = _templates(request).render(
        request,
        "platform/verify_sent.html",
        {"identity": identity, "principal": None},
    )
    return HTMLResponse(html)


# --------------------------------------------------------- verify email


@router.get("/platform/verify-email", response_class=HTMLResponse)
async def verify_email(
    request: Request,
    token: str,
    db: AsyncSession = Depends(get_platform_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    try:
        payload = verify_token(
            settings.app_secret_key,
            TokenPurpose.EMAIL_VERIFY,
            token,
            max_age_seconds=VERIFY_TOKEN_MAX_AGE_SECONDS,
        )
    except ExpiredToken:
        html = _templates(request).render(
            request,
            "platform/verify_email.html",
            {
                "success": False,
                "message": "Odkaz pro ověření vypršel. Můžete si zažádat o nový.",
                "principal": None,
            },
        )
        return HTMLResponse(html, status_code=400)
    except InvalidToken:
        html = _templates(request).render(
            request,
            "platform/verify_email.html",
            {
                "success": False,
                "message": "Odkaz pro ověření je neplatný.",
                "principal": None,
            },
        )
        return HTMLResponse(html, status_code=400)

    try:
        identity_uuid = UUID(str(payload["identity_id"]))
    except (KeyError, ValueError):
        html = _templates(request).render(
            request,
            "platform/verify_email.html",
            {
                "success": False,
                "message": "Odkaz pro ověření je poškozený.",
                "principal": None,
            },
        )
        return HTMLResponse(html, status_code=400)

    try:
        await mark_email_verified(db, identity_id=identity_uuid)
    except PlatformError:
        html = _templates(request).render(
            request,
            "platform/verify_email.html",
            {
                "success": False,
                "message": "Účet k ověření nebyl nalezen.",
                "principal": None,
            },
        )
        return HTMLResponse(html, status_code=404)

    await db.commit()

    # Look up any plan the user pre-selected on /pricing?plan=pro so
    # the success page can point them straight into checkout instead
    # of a generic "continue to the portal" dead-end.
    selected_plan, tenant_slug = await _lookup_signup_selected_plan(db, identity_uuid)

    html = _templates(request).render(
        request,
        "platform/verify_email.html",
        {
            "success": True,
            "message": "E-mail byl úspěšně ověřen.",
            "selected_plan": selected_plan,
            "tenant_slug": tenant_slug,
            "principal": None,
        },
    )
    return HTMLResponse(html)


async def _lookup_signup_selected_plan(
    db: AsyncSession, identity_id: UUID
) -> tuple[str | None, str | None]:
    """Return the ``selected_plan`` the user picked on /pricing (or None).

    Resolves the identity's first staff membership → that tenant →
    ``tenant.settings.get("selected_plan")``. Used only to power the
    post-verification "finish checkout" CTA; treats any error as
    "no plan selected" so a missing row never blocks verification.
    """
    from app.models.tenant import Tenant
    from app.platform.service import list_memberships_for_identity

    try:
        memberships = await list_memberships_for_identity(db, identity_id=identity_id)
    except Exception:
        return None, None

    for m in memberships:
        if m.user_id is None:
            continue
        tenant = (
            await db.execute(select(Tenant).where(Tenant.id == m.tenant_id))
        ).scalar_one_or_none()
        if tenant is None:
            continue
        chosen = (tenant.settings or {}).get("selected_plan")
        if chosen in {"starter", "pro"}:
            return chosen, tenant.slug
        return None, tenant.slug
    return None, None


# ----------------------------------------------------------- resend link


@router.post("/platform/verify-resend", response_class=HTMLResponse)
@rate_limit("3/5 minutes")
async def resend_verification(
    request: Request,
    background_tasks: BackgroundTasks,
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_platform_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    if identity.email_verified_at is not None:
        return RedirectResponse(
            url="/platform/select-tenant", status_code=status.HTTP_303_SEE_OTHER
        )
    # Look up the identity's first staff-owned tenant so the resent
    # email renders the company name (round-3 Backend P2 — previously
    # ``company_name=""`` produced "Vítejte ve firmě " with a trailing
    # blank).
    company_name = await _company_name_for_identity(db, identity.id)
    verify_url = _build_verify_url(settings, identity.id)
    background_tasks.add_task(
        send_email_verification,
        request.app.state.email_sender,
        to=identity.email,
        full_name=identity.full_name,
        company_name=company_name,
        verify_url=verify_url,
    )
    return RedirectResponse(url="/platform/verify-sent", status_code=status.HTTP_303_SEE_OTHER)


async def _company_name_for_identity(db: AsyncSession, identity_id: UUID) -> str:
    """Best-effort lookup of a display-worthy tenant name for the resend email.

    Returns an empty string (the only safe fallback) on any failure —
    we never block a resend just because we can't render the greeting
    prettily.
    """
    from app.models.tenant import Tenant
    from app.platform.service import list_memberships_for_identity

    try:
        memberships = await list_memberships_for_identity(db, identity_id=identity_id)
    except Exception:
        return ""
    for m in memberships:
        if m.user_id is None:
            continue
        tenant = (
            await db.execute(select(Tenant).where(Tenant.id == m.tenant_id))
        ).scalar_one_or_none()
        if tenant is not None:
            return tenant.name
    return ""
