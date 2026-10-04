"""Self-signup, email verification, and onboarding routes.

Lives under the platform package because these flows only make sense
when the hosted SaaS layer is turned on (``FEATURE_PLATFORM=true``).
Core self-hosted builds never mount this router.
"""

from __future__ import annotations

from types import SimpleNamespace
import re
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.i18n import t as _t
from app.logging import get_logger
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
) -> Response:
    """Show the registration form (or bounce to tenant picker if logged in)."""
    if identity is not None:
        return RedirectResponse(
            url="/platform/select-tenant", status_code=status.HTTP_303_SEE_OTHER
        )
    ref, ref_tenant = _signup_ref_from_request(request)
    html = _templates(request).render(
        request,
        "platform/signup.html",
        {
            "errors": {},
            "form": {"ref": ref, "ref_t": ref_tenant},
            "principal": None,
        },
    )
    return HTMLResponse(html)


# Attribution for the "Powered by Assoluto" footer (MKT-9). The footer
# links to ``<apex>/?ref=portal&t=<tenant slug>``; the visitor then
# clicks through to this page. No cookie is set: the pair is read from
# this page's own query string, or from the same-origin ``Referer`` (the
# browser default ``strict-origin-when-cross-origin`` policy keeps the
# full URL for same-origin navigations), and carried in hidden fields.
_ALLOWED_REFS = frozenset({"portal"})
_REF_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")


def _clean_ref(ref: str, ref_tenant: str) -> tuple[str, str]:
    ref = (ref or "").strip().lower()
    ref_tenant = (ref_tenant or "").strip().lower()
    if ref not in _ALLOWED_REFS:
        return "", ""
    if not _REF_SLUG_RE.fullmatch(ref_tenant):
        ref_tenant = ""
    return ref, ref_tenant


def _signup_ref_from_request(request: Request) -> tuple[str, str]:
    params = request.query_params
    if params.get("ref"):
        return _clean_ref(params.get("ref", ""), params.get("t", ""))
    referer = request.headers.get("referer") or ""
    if not referer:
        return "", ""
    parsed = urlsplit(referer)
    # Same-origin only — a foreign site must not be able to plant a ref.
    if parsed.netloc and parsed.netloc != request.url.netloc:
        return "", ""
    query = parse_qs(parsed.query)
    return _clean_ref((query.get("ref") or [""])[0], (query.get("t") or [""])[0])


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
    website: str = Form(""),
    ref: str = Form(""),
    ref_t: str = Form(""),
    db: AsyncSession = Depends(get_platform_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    # Honeypot — the ``website`` field is hidden from real users via
    # CSS ``display:none`` and an ``aria-hidden`` label. Bots that
    # naively fill every field will trip it; pretend the signup
    # succeeded so the bot doesn't iterate. Real conversions are
    # never affected because no human sees the field. Logged at info
    # for ops visibility.
    if website.strip():
        get_logger("app.platform.signup").info(
            "signup.honeypot_tripped",
            length=len(website),
        )
        html = _templates(request).render(
            request,
            "platform/verify_sent.html",
            # The template reads ``identity.email``; passing a bare
            # ``email`` used to raise UndefinedError -> 500, which told
            # the bot it had been caught (audit BE-07).
            {
                "identity": SimpleNamespace(email=owner_email or ""),
                "email": owner_email or "",
                "principal": None,
            },
        )
        return HTMLResponse(html)

    form_raw = {
        "company_name": company_name,
        "slug": slug,
        "owner_email": owner_email,
        "owner_full_name": owner_full_name,
        # Keep the plan picked on /pricing across a validation error —
        # without it the re-rendered form lost ``?plan=pro`` and the
        # corrected submit silently started a Starter trial (UX-27).
        "plan": _safe_plan_code(plan),
        "ref": ref,
        "ref_t": ref_t,
        # Intentionally not echoing the password back.
    }

    # Throwaway-inbox domains: the same list the contact form drops
    # silently. Here a real person may be behind it, so say why instead
    # of pretending success — and never send a verification mail to a
    # mailbox that will be gone tomorrow (BIZ-09). The random-local-part
    # heuristic the contact form also uses is NOT applied: it matches
    # real addresses like ``novakjosef1985@…``, and an unverified signup
    # can no longer reach nurture mail or the funnel anyway.
    from app.security.contact_filter import is_disposable_email

    if is_disposable_email(owner_email):
        get_logger("app.platform.signup").info(
            "signup.disposable_email_rejected",
            domain=(owner_email or "").rsplit("@", 1)[-1],
        )
        html = _templates(request).render(
            request,
            "platform/signup.html",
            {
                "errors": {
                    "owner_email": _t(
                        request,
                        "Please use a permanent work email address — we send order "
                        "notifications there.",
                    )
                },
                "form": form_raw,
                "principal": None,
            },
        )
        return HTMLResponse(html, status_code=400)

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
                "errors": {exc.field: exc.localized(lambda m: _t(request, m))},
                "form": form_raw,
                "principal": None,
            },
        )
        return HTMLResponse(html, status_code=400)

    # Per-email signup rate limit. Per-IP limit @rate_limit("10/15
    # minutes") above is primary defence; this catches an attacker who
    # rotates IPs but keeps hammering a single victim email (Identity
    # has UNIQUE(email) so a duplicate would fail at DB level anyway,
    # but we want to reject before we enqueue the verify mail to
    # prevent inbox spam on the second attempt).
    from app.security.email_throttle import SIGNUP_THROTTLE

    if not SIGNUP_THROTTLE.allow(form.owner_email):
        html = _templates(request).render(
            request,
            "platform/signup.html",
            {
                "errors": {
                    "owner_email": _t(
                        request,
                        "This address already tried to sign up today. "
                        "Try again tomorrow or reset your password.",
                    )
                },
                "form": form_raw,
                "principal": None,
            },
        )
        return HTMLResponse(html, status_code=429)

    # 2) Provision tenant + owner user + Identity. Capture the consent
    # version + source IP so we have a defensible GDPR audit trail.
    # The IP goes through the same trust-proxies plumbing used by the
    # rate limiter — we read the real client, not the reverse-proxy
    # container.
    from app.security.rate_limit import _client_ip as _real_client_ip

    # The plan the visitor clicked on /pricing. It drives BOTH the trial
    # subscription created below and the "Finish setup" CTA later, so it
    # has to be resolved before the tenant is created — clicking Pro used
    # to still start a Starter trial (3 users / 20 contacts).
    selected_plan = _safe_plan_code(plan)

    try:
        tenant, _owner, identity = await signup_tenant(
            db,
            company_name=form.company_name,
            slug=form.slug,
            owner_email=form.owner_email,
            owner_full_name=form.owner_full_name,
            owner_password=form.password,
            consent_ip=_real_client_ip(request),
            consent_version=settings.legal_doc_version,
            plan_code=selected_plan or "starter",
        )
    except DuplicateTenantSlug:
        html = _templates(request).render(
            request,
            "platform/signup.html",
            {
                "errors": {"slug": _t(request, "This subdomain is already taken.")},
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
                    "owner_email": _t(
                        request, "An account with this email already exists. Please sign in."
                    )
                },
                "form": form_raw,
                "principal": None,
            },
        )
        return HTMLResponse(html, status_code=400)
    except PlatformError as exc:
        error_label = _t(request, "Error")
        html = _templates(request).render(
            request,
            "platform/signup.html",
            {
                "errors": {"company_name": f"{error_label}: {exc}"},
                "form": form_raw,
                "principal": None,
            },
        )
        return HTMLResponse(html, status_code=400)

    # Preserve the plan the user clicked on the pricing page so verify-sent /
    # verify-email can surface a "Finish setup" CTA that leads into checkout.
    if selected_plan:
        tenant_settings = dict(tenant.settings or {})
        tenant_settings["selected_plan"] = selected_plan
        tenant.settings = tenant_settings
        await db.flush()

    # Viral-loop attribution (MKT-9): which customer portal sent them.
    clean_ref, clean_ref_t = _clean_ref(ref, ref_t)
    if clean_ref:
        tenant_settings = dict(tenant.settings or {})
        tenant_settings["signup_ref"] = {"ref": clean_ref, "t": clean_ref_t}
        tenant.settings = tenant_settings
        await db.flush()
        get_logger("app.platform.signup").info(
            "signup.attributed", ref=clean_ref, referring_tenant=clean_ref_t
        )

    # 3) Commit BEFORE scheduling the email task (BackgroundTasks run before
    # the request-scoped session commit; see CLAUDE.md for the pattern).
    await db.commit()

    verify_url = _build_verify_url(settings, identity.id)
    sender = request.app.state.email_sender
    locale = getattr(request.state, "locale", settings.default_locale)
    background_tasks.add_task(
        send_email_verification,
        sender,
        to=identity.email,
        full_name=identity.full_name,
        company_name=tenant.name,
        verify_url=verify_url,
        locale=locale,
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
            session_version=identity.session_version,
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
                "message": _t(
                    request, "The verification link has expired. You can request a new one."
                ),
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
                "message": _t(request, "The verification link is invalid."),
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
                "message": _t(request, "The verification link is malformed."),
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
                "message": _t(request, "The account to verify was not found."),
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
            "message": _t(request, "Your email address has been verified."),
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


# --------- public "check your email" page (for users not yet logged in) ---------


@router.get("/platform/check-email", response_class=HTMLResponse)
async def check_email_form(
    request: Request,
    email: str = "",
) -> HTMLResponse:
    """Public page shown when a user tries to log in but their Identity
    is still unverified. Pre-fills the resend form with the email they
    typed at login (passed via query param) so they don't have to type
    it again.
    """
    html = _templates(request).render(
        request,
        "platform/check_email.html",
        {"email": email, "principal": None, "sent": False},
    )
    return HTMLResponse(html)


@router.post("/platform/check-email", response_class=HTMLResponse)
@rate_limit("5/15 minutes")
async def check_email_resend(
    request: Request,
    background_tasks: BackgroundTasks,
    email: str = Form(...),
    db: AsyncSession = Depends(get_platform_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Public resend endpoint — accepts an email, looks up the Identity,
    sends a fresh verify link only if unverified. Always returns the
    "we sent it" page regardless of whether anything actually happened
    so we don't leak which emails own accounts (email enumeration
    defence). Rate-limited per IP.
    """
    email_norm = (email or "").strip().lower()

    from app.security.email_throttle import VERIFY_RESEND_THROTTLE

    sent = False
    if email_norm and VERIFY_RESEND_THROTTLE.allow(email_norm):
        identity = (
            await db.execute(select(Identity).where(Identity.email == email_norm))
        ).scalar_one_or_none()
        if identity is not None and identity.email_verified_at is None:
            company_name = await _company_name_for_identity(db, identity.id)
            verify_url = _build_verify_url(settings, identity.id)
            locale = getattr(request.state, "locale", settings.default_locale)
            # Release the DB connection before the SMTP work: FastAPI runs
            # background tasks BEFORE dependency cleanup, so without this the
            # session would sit "idle in transaction" for the whole send
            # (audit BE-08; CLAUDE.md §2).
            await db.commit()
            background_tasks.add_task(
                send_email_verification,
                request.app.state.email_sender,
                to=identity.email,
                full_name=identity.full_name,
                company_name=company_name,
                verify_url=verify_url,
                locale=locale,
            )
            sent = True

    html = _templates(request).render(
        request,
        "platform/check_email.html",
        {"email": email_norm, "principal": None, "sent": True, "_actually_sent": sent},
    )
    return HTMLResponse(html)


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
    from app.security.email_throttle import VERIFY_RESEND_THROTTLE

    if not VERIFY_RESEND_THROTTLE.allow(identity.email):
        # Silently redirect — the endpoint's UX contract is "show
        # verify-sent page again", no need to leak the throttle state.
        return RedirectResponse(url="/platform/verify-sent", status_code=status.HTTP_303_SEE_OTHER)
    # Look up the identity's first staff-owned tenant so the resent
    # email renders the company name (round-3 Backend P2 — previously
    # ``company_name=""`` produced "Vítejte ve firmě " with a trailing
    # blank).
    company_name = await _company_name_for_identity(db, identity.id)
    verify_url = _build_verify_url(settings, identity.id)
    locale = getattr(request.state, "locale", settings.default_locale)
    # Release the DB connection before the SMTP work: FastAPI runs
    # background tasks BEFORE dependency cleanup, so without this the
    # session would sit "idle in transaction" for the whole send
    # (audit BE-08; CLAUDE.md §2).
    await db.commit()
    background_tasks.add_task(
        send_email_verification,
        request.app.state.email_sender,
        to=identity.email,
        full_name=identity.full_name,
        company_name=company_name,
        verify_url=verify_url,
        locale=locale,
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
