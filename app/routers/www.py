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

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.config import Settings, get_settings
from app.security.csrf import verify_csrf

router = APIRouter(tags=["www"], dependencies=[Depends(verify_csrf)])


def _templates(request: Request):
    return request.app.state.templates


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
    if not name or not email or not message:
        html = _templates(request).render(
            request,
            "www/contact.html",
            {
                "principal": None,
                "submitted": False,
                "error": "Vyplňte prosím všechna pole.",
                "form": {"name": name, "email": email, "message": message},
            },
        )
        return HTMLResponse(html, status_code=400)

    # Fire-and-forget: use the existing email sender to mail us the message.
    from app.tasks.email_tasks import _safe_send

    sender = request.app.state.email_sender
    support_to = settings.smtp_from or "support@localhost"
    body_text = f"Jméno: {name}\nE-mail: {email}\n\n{message}\n"
    body_html = (
        f"<p><strong>Jméno:</strong> {name}</p>"
        f"<p><strong>E-mail:</strong> {email}</p>"
        f"<p>{message}</p>"
    )
    background_tasks.add_task(
        _safe_send,
        sender,
        "contact",
        support_to,
        f"[SME Portal] Kontakt od {name}",
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
async def terms(request: Request) -> HTMLResponse:
    html = _templates(request).render(request, "www/terms.html", {"principal": None})
    return HTMLResponse(html)


@router.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request) -> HTMLResponse:
    html = _templates(request).render(request, "www/privacy.html", {"principal": None})
    return HTMLResponse(html)
