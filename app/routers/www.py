"""Public marketing routes: features, pricing, self-hosted guide, legal.

These pages are served at the **apex** domain — they are not tenant-specific,
so no tenant resolution is done. They're available regardless of
``FEATURE_PLATFORM`` because the legal pages (``/terms``, ``/privacy``) are
linked from the signup form, which itself is platform-gated, and the
marketing pages are harmless on a self-hosted deployment (they just advertise
the hosted option; the operator can hide them with an nginx rule if they
prefer).
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from markupsafe import escape

from app.config import Settings, get_settings
from app.security.csrf import verify_csrf
from app.security.rate_limit import limit as rate_limit

# Cap the contact-form message to keep the transactional email sender
# happy (most providers start scoring messages above ~64 KB as spam)
# and to close off abuse of the endpoint as a mass-mail relay.
CONTACT_MESSAGE_MAX_CHARS = 4000
CONTACT_NAME_MAX_CHARS = 120

router = APIRouter(tags=["www"], dependencies=[Depends(verify_csrf)])


def _templates(request: Request):
    return request.app.state.templates


def _operator_context(settings: Settings) -> dict:
    """Return the operator identity fields for legal templates."""
    return {
        "operator_name": settings.platform_operator_name,
        "operator_ico": settings.platform_operator_ico,
        "operator_address": settings.platform_operator_address,
        "operator_email": settings.platform_operator_email,
    }


def _require_operator_identity(settings: Settings) -> None:
    """Legal pages must only serve when the operator identity is configured.

    Publishing a half-filled Terms of Service that a user could legally
    accept against an unnamed party is a compliance risk we close off
    with a hard 404 when any of name / IČO / address is missing.
    """
    if not settings.operator_identity_complete:
        raise HTTPException(
            status_code=404,
            detail=(
                "Legal pages are not configured on this deployment. "
                "Set PLATFORM_OPERATOR_NAME / _ICO / _ADDRESS."
            ),
        )


@router.get("/features", response_class=HTMLResponse)
async def features(request: Request) -> HTMLResponse:
    html = _templates(request).render(request, "www/features.html", {"principal": None})
    return HTMLResponse(html)


@router.get("/pricing", response_class=HTMLResponse)
async def pricing(request: Request) -> HTMLResponse:
    html = _templates(request).render(request, "www/pricing.html", {"principal": None})
    return HTMLResponse(html)


@router.get("/self-hosted", response_class=HTMLResponse)
async def self_hosted(request: Request) -> HTMLResponse:
    html = _templates(request).render(request, "www/self_hosted.html", {"principal": None})
    return HTMLResponse(html)


@router.get("/contact", response_class=HTMLResponse)
async def contact_form(request: Request) -> HTMLResponse:
    html = _templates(request).render(
        request,
        "www/contact.html",
        {"principal": None, "submitted": False, "error": None},
    )
    return HTMLResponse(html)


@router.post("/contact", response_class=HTMLResponse)
@rate_limit("5/15 minutes")
async def contact_submit(
    request: Request,
    background_tasks: BackgroundTasks,
    name: str = Form(...),
    email: str = Form(...),
    message: str = Form(...),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Capture a contact-form submission and forward it to the support inbox.

    Deliberately minimal — no CAPTCHA, no rate limiting beyond what nginx
    provides. If volume becomes a problem, add Turnstile or hCaptcha.
    """
    name = (name or "").strip()
    email = (email or "").strip()
    message = (message or "").strip()

    def _reject(err: str) -> HTMLResponse:
        html = _templates(request).render(
            request,
            "www/contact.html",
            {
                "principal": None,
                "submitted": False,
                "error": err,
                "form": {"name": name, "email": email, "message": message},
            },
        )
        return HTMLResponse(html, status_code=400)

    if not name or not email or not message:
        return _reject("Vyplňte prosím všechna pole.")
    if len(name) > CONTACT_NAME_MAX_CHARS:
        return _reject(f"Jméno může mít nejvýše {CONTACT_NAME_MAX_CHARS} znaků.")
    if len(message) > CONTACT_MESSAGE_MAX_CHARS:
        return _reject(f"Zpráva může mít nejvýše {CONTACT_MESSAGE_MAX_CHARS} znaků.")
    try:
        from email_validator import EmailNotValidError
        from email_validator import validate_email as _ve

        _ve(email, check_deliverability=False)
    except EmailNotValidError:
        return _reject("Zadaný e-mail není platný.")

    # Fire-and-forget: use the existing email sender to mail us the message.
    # Every piece of user input is HTML-escaped before being interpolated
    # into the email body (the Subject header is encoded by EmailMessage).
    from app.tasks.email_tasks import _safe_send

    sender = request.app.state.email_sender
    support_to = settings.smtp_from or "support@localhost"
    safe_name = escape(name)
    safe_email = escape(email)
    # Preserve newlines in the HTML view by replacing them after escape.
    safe_message_html = str(escape(message)).replace("\n", "<br>")
    body_text = f"Jméno: {name}\nE-mail: {email}\n\n{message}\n"
    body_html = (
        f"<p><strong>Jméno:</strong> {safe_name}</p>"
        f"<p><strong>E-mail:</strong> {safe_email}</p>"
        f"<p>{safe_message_html}</p>"
    )
    background_tasks.add_task(
        _safe_send,
        sender,
        "contact",
        support_to,
        f"[Assoluto] Kontakt od {name}",
        body_html,
        body_text,
    )

    html = _templates(request).render(
        request,
        "www/contact.html",
        {"principal": None, "submitted": True, "error": None},
    )
    return HTMLResponse(html)


@router.get("/terms", response_class=HTMLResponse)
async def terms(request: Request, settings: Settings = Depends(get_settings)) -> HTMLResponse:
    _require_operator_identity(settings)
    html = _templates(request).render(
        request,
        "www/terms.html",
        {"principal": None, **_operator_context(settings)},
    )
    return HTMLResponse(html)


@router.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request, settings: Settings = Depends(get_settings)) -> HTMLResponse:
    _require_operator_identity(settings)
    html = _templates(request).render(
        request,
        "www/privacy.html",
        {"principal": None, **_operator_context(settings)},
    )
    return HTMLResponse(html)
