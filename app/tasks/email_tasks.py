"""Background email tasks.

Every public function here takes an :class:`EmailSender` rather than
reading a global so that tests can plug in a :class:`CaptureSender`
without monkey-patching.

### Locale handling

Single-recipient emails (invitations, verification, password reset)
take a ``locale`` keyword. The caller is responsible for resolving it
via :func:`app.services.locale_service.resolve_email_locale` before
scheduling the task — the service layer owns the
recipient/customer/tenant lookup and keeps this task module free of
DB plumbing.

Multi-recipient emails (order notifications fanning out to tenant
staff or to all contacts of a customer) take ``recipients_with_locale``,
a sequence of ``(email, locale_or_none)`` tuples. Each recipient gets
their own render so a US contact and a Czech staff user can both be
on the same notification list and each see it in their preferred
language.
"""

from __future__ import annotations

import time
from collections.abc import Iterable

from app.email.sender import EmailSender, render_email
from app.logging import get_logger
from app.models.enums import OrderStatus

log = get_logger("app.tasks.email")

# Retry a failed send this many times before giving up. Kept small so
# the background-task queue doesn't build up on a sustained outage —
# if SMTP is down for more than a few minutes, operator intervention
# is warranted. Backoff is exponential on the same-ish order as TCP's
# typical retransmit window.
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 2.0


# English msgids — the template's ``_(status_label)`` translates at
# render time using the recipient's locale. Keep the CS text OUT of
# this module so one catalogue covers every surface.
STATUS_LABELS: dict[OrderStatus, str] = {
    OrderStatus.DRAFT: "Draft",
    OrderStatus.SUBMITTED: "Submitted",
    OrderStatus.QUOTED: "Quoted",
    OrderStatus.CONFIRMED: "Confirmed",
    OrderStatus.IN_PRODUCTION: "In production",
    OrderStatus.READY: "Ready",
    OrderStatus.DELIVERED: "Delivered",
    OrderStatus.CLOSED: "Closed",
    OrderStatus.CANCELLED: "Cancelled",
}


def _safe_error_summary(exc: Exception) -> str:
    """Sanitise an exception message for inclusion in structured logs.

    SMTP libraries can echo bits of the rendered email body or DSN
    headers in their error reprs — and our reset / invite mails carry
    URL-embedded one-shot tokens that must never leak into the log
    pipeline. Strips anything that looks like a URL, query parameter,
    JWT-shape token, raw hex secret, or generic long base64-ish blob,
    then truncates.
    """
    import re

    cleaned = str(exc)
    # 1. Full URLs (http(s)://… up to next whitespace).
    cleaned = re.sub(r"https?://\S+", "[url]", cleaned)
    # 2. JWT-shape three-segment tokens.
    cleaned = re.sub(
        r"[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}",
        "[jwt]",
        cleaned,
    )
    # 3. ``key=value`` blobs with ≥12-char value (handles existing
    #    query-string vector and Bearer-with-equals).
    cleaned = re.sub(r"=([A-Za-z0-9_\-]{12,})", "=[redacted]", cleaned)
    # 4. Bearer / token / authorization header form: ``Bearer <token>``,
    #    ``Authorization: <token>``, ``X-Token: <token>``. Match the
    #    keyword (any case) plus optional colon/space, then redact the
    #    token-like run that follows.
    cleaned = re.sub(
        r"(?i)\b(bearer|authorization|x[-_]?token|token)\b\s*[:= ]\s*([A-Za-z0-9_\-]{16,})",
        r"\1 [redacted]",
        cleaned,
    )
    # 5. Standalone long hex blobs (32+ hex chars).
    cleaned = re.sub(r"\b[A-Fa-f0-9]{32,}\b", "[hex]", cleaned)
    return cleaned[:160]


def _safe_send(
    sender: EmailSender,
    kind: str,
    to: str,
    subject: str,
    html: str,
    text: str,
) -> None:
    """Send one email, retrying briefly on transient failures.

    Fire-and-forget: the caller is a FastAPI BackgroundTask running
    after the request has been served, so exceptions must not escape.
    We retry up to :data:`_MAX_ATTEMPTS` times with exponential backoff
    — this catches the SMTP-relay blips that would otherwise drop a
    single password-reset or invite mail on the floor. Permanent
    errors (auth failure, invalid recipient) fail on every attempt and
    end up in the ``email.failed`` log with ``attempts`` set.

    Each attempt logs both start and outcome with a duration so the
    operator can tell "Brevo took 9.5 s" apart from "DNS resolution
    bounced in 30 ms" when chasing the next silent-drop incident.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        log.info("email.attempt", kind=kind, to=to, attempt=attempt)
        started = time.monotonic()
        try:
            sender.send(to=to, subject=subject, html=html, text=text)
            duration_ms = int((time.monotonic() - started) * 1000)
            log.info(
                "email.sent",
                kind=kind,
                to=to,
                attempts=attempt,
                duration_ms=duration_ms,
            )
            return
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            last_exc = exc
            if attempt < _MAX_ATTEMPTS:
                # Backoff: 2s, 4s, 8s …
                time.sleep(_BACKOFF_BASE_SECONDS**attempt)
                log.warning(
                    "email.retry",
                    kind=kind,
                    to=to,
                    attempt=attempt,
                    duration_ms=duration_ms,
                    error_class=type(exc).__name__,
                    error=_safe_error_summary(exc),
                )
    log.error(
        "email.failed",
        kind=kind,
        to=to,
        attempts=_MAX_ATTEMPTS,
        error_class=type(last_exc).__name__ if last_exc else "unknown",
        error=_safe_error_summary(last_exc) if last_exc else "",
    )


def _render_and_send(
    sender: EmailSender,
    kind: str,
    template: str,
    to: str,
    context: dict,
    locale: str | None,
) -> None:
    """Render ``template`` in ``locale`` and hand it to the sender."""
    rendered = render_email(template, context, locale=locale)
    _safe_send(sender, kind, to, rendered.subject, rendered.html, rendered.text)


def send_invitation(
    sender: EmailSender,
    *,
    to: str,
    tenant_name: str,
    customer_name: str,
    contact_name: str,
    invite_url: str,
    locale: str | None = None,
) -> None:
    """Send an invitation email to a new customer contact."""
    _render_and_send(
        sender,
        "invitation",
        "invitation",
        to,
        {
            "tenant_name": tenant_name,
            "customer_name": customer_name,
            "contact_name": contact_name,
            "invite_url": invite_url,
        },
        locale,
    )


def send_email_verification(
    sender: EmailSender,
    *,
    to: str,
    full_name: str,
    company_name: str,
    verify_url: str,
    locale: str | None = None,
) -> None:
    """Send the platform signup email-verification link."""
    _render_and_send(
        sender,
        "email_verification",
        "email_verification",
        to,
        {
            "full_name": full_name,
            "company_name": company_name,
            "verify_url": verify_url,
        },
        locale,
    )


def send_staff_invitation(
    sender: EmailSender,
    *,
    to: str,
    tenant_name: str,
    invitee_name: str,
    invite_url: str,
    locale: str | None = None,
) -> None:
    """Send an invitation email to a new tenant staff user."""
    _render_and_send(
        sender,
        "staff_invitation",
        "staff_invitation",
        to,
        {
            "tenant_name": tenant_name,
            "invitee_name": invitee_name,
            "invite_url": invite_url,
        },
        locale,
    )


def send_password_reset(
    sender: EmailSender,
    *,
    to: str,
    tenant_name: str,
    full_name: str,
    reset_url: str,
    locale: str | None = None,
) -> None:
    """Send a password-reset e-mail carrying a one-shot URL."""
    _render_and_send(
        sender,
        "password_reset",
        "password_reset",
        to,
        {
            "tenant_name": tenant_name,
            "full_name": full_name,
            "reset_url": reset_url,
        },
        locale,
    )


RecipientLocales = Iterable[tuple[str, str | None]]


def send_order_comment(
    sender: EmailSender,
    *,
    recipients_with_locale: RecipientLocales,
    tenant_name: str,
    order_number: str,
    order_title: str,
    order_url: str,
    author_name: str,
    body_excerpt: str,
) -> None:
    ctx = {
        "tenant_name": tenant_name,
        "order_number": order_number,
        "order_title": order_title,
        "order_url": order_url,
        "author_name": author_name,
        "body_excerpt": body_excerpt,
    }
    for to, locale in recipients_with_locale:
        _render_and_send(sender, "order_comment", "order_comment", to, ctx, locale)


def send_order_submitted(
    sender: EmailSender,
    *,
    recipients_with_locale: RecipientLocales,
    tenant_name: str,
    customer_name: str,
    order_number: str,
    order_title: str,
    order_url: str,
) -> None:
    """Notify tenant staff that a customer just submitted an order."""
    ctx = {
        "tenant_name": tenant_name,
        "customer_name": customer_name,
        "order_number": order_number,
        "order_title": order_title,
        "order_url": order_url,
    }
    for to, locale in recipients_with_locale:
        _render_and_send(sender, "order_submitted", "order_submitted", to, ctx, locale)


def send_order_status_changed(
    sender: EmailSender,
    *,
    recipients_with_locale: RecipientLocales,
    tenant_name: str,
    order_number: str,
    order_title: str,
    order_url: str,
    to_status: OrderStatus,
) -> None:
    """Notify customer contacts that an order's status changed."""
    label = STATUS_LABELS.get(to_status, to_status.value)
    ctx = {
        "tenant_name": tenant_name,
        "order_number": order_number,
        "order_title": order_title,
        "order_url": order_url,
        "status_label": label,
    }
    for to, locale in recipients_with_locale:
        _render_and_send(sender, "order_status_changed", "order_status_changed", to, ctx, locale)
