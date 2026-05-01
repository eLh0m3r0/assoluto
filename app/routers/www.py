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
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from markupsafe import escape

from app.config import Settings, get_settings
from app.i18n import t as _t
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
        "operator_dic": settings.platform_operator_dic,
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
        return _reject(_t(request, "Please fill in all fields."))
    if len(name) > CONTACT_NAME_MAX_CHARS:
        return _reject(
            _t(request, "Name may be at most {n} characters.").format(n=CONTACT_NAME_MAX_CHARS)
        )
    if len(message) > CONTACT_MESSAGE_MAX_CHARS:
        return _reject(
            _t(request, "Message may be at most {n} characters.").format(
                n=CONTACT_MESSAGE_MAX_CHARS
            )
        )
    try:
        from email_validator import EmailNotValidError
        from email_validator import validate_email as _ve

        _ve(email, check_deliverability=False)
    except EmailNotValidError:
        return _reject(_t(request, "Email address is not valid."))

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


@router.get("/cookies", response_class=HTMLResponse)
async def cookies_policy(
    request: Request, settings: Settings = Depends(get_settings)
) -> HTMLResponse:
    """EU ePrivacy / CZ Act 127/2005 §89 cookies disclosure. Static.

    We only use strictly-necessary cookies (session, CSRF, locale,
    theme) so this page explains the analysis and lists each cookie
    rather than asking for consent.
    """
    _require_operator_identity(settings)
    html = _templates(request).render(
        request,
        "www/cookies.html",
        {"principal": None, **_operator_context(settings)},
    )
    return HTMLResponse(html)


@router.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
async def robots_txt(request: Request) -> PlainTextResponse:
    """Bot guidance: allow public marketing pages, disallow app/platform/auth
    surfaces. Sitemap URL is generated from the request's scheme+host so it
    works both on assoluto.eu and local dev without extra config.
    """
    base = f"{request.url.scheme}://{request.url.netloc}"
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /app\n"
        "Disallow: /app/\n"
        "Disallow: /auth/\n"
        "Disallow: /platform/admin\n"
        "Disallow: /platform/admin/\n"
        "Disallow: /platform/login\n"
        "Disallow: /platform/signup\n"
        "Disallow: /platform/password-reset\n"
        "\n"
        f"Sitemap: {base}/sitemap.xml\n"
    )
    return PlainTextResponse(body, media_type="text/plain")


@router.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml(request: Request) -> Response:
    """Sitemap of public marketing pages.

    Legal pages (``/terms``, ``/privacy``, ``/cookies``, ``/imprint``) are
    only listed when the operator identity is configured — otherwise they
    404 and would poison the sitemap.
    """
    from app.config import get_settings as _gs

    settings = _gs()
    base = f"{request.url.scheme}://{request.url.netloc}"
    pages: list[tuple[str, str]] = [
        ("/", "1.0"),
        ("/features", "0.9"),
        ("/pricing", "0.9"),
        ("/self-hosted", "0.7"),
        ("/contact", "0.6"),
    ]
    if settings.operator_identity_complete:
        pages += [
            ("/terms", "0.3"),
            ("/privacy", "0.3"),
            ("/cookies", "0.3"),
            ("/imprint", "0.3"),
        ]
    # hreflang alternates per <url>: same URL serves all three locales via
    # Accept-Language. Without these tags Google indexes whichever locale
    # Googlebot's data centre lands on. UX audit 2026-05-01-1335 F-UX-010.
    locales = ("cs", "en", "de")

    def _entry(path: str, priority: str) -> str:
        loc = f"{base}{path}"
        alts = "\n".join(
            f'    <xhtml:link rel="alternate" hreflang="{lang}" href="{loc}"/>' for lang in locales
        )
        alts += f'\n    <xhtml:link rel="alternate" hreflang="x-default" href="{loc}"/>'
        return f"  <url><loc>{loc}</loc><priority>{priority}</priority>\n{alts}\n  </url>"

    urls = "\n".join(_entry(path, p) for path, p in pages)
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"\n'
        '        xmlns:xhtml="http://www.w3.org/1999/xhtml">\n'
        f"{urls}\n"
        "</urlset>\n"
    )
    return Response(body, media_type="application/xml")


@router.get("/imprint", response_class=HTMLResponse)
async def imprint(request: Request, settings: Settings = Depends(get_settings)) -> HTMLResponse:
    """CZ Act 480/2004 §8 operator-identity disclosure.

    Required on every Czech commercial website: legal name, seat,
    company ID (IČO), tax ID (DIČ), contact. Populated from
    ``PLATFORM_OPERATOR_*`` env vars via ``_operator_context``.
    """
    _require_operator_identity(settings)
    html = _templates(request).render(
        request,
        "www/imprint.html",
        {"principal": None, **_operator_context(settings)},
    )
    return HTMLResponse(html)
