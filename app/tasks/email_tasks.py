"""Background email tasks.

Every public function here takes an `EmailSender` rather than reading a
global so that tests can plug in a `CaptureSender` without monkey-patching.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.email.sender import EmailSender, render_email
from app.logging import get_logger
from app.models.enums import OrderStatus

log = get_logger("app.tasks.email")


STATUS_LABELS: dict[OrderStatus, str] = {
    OrderStatus.DRAFT: "Koncept",
    OrderStatus.SUBMITTED: "Odesláno",
    OrderStatus.QUOTED: "Nacenění",
    OrderStatus.CONFIRMED: "Potvrzeno",
    OrderStatus.IN_PRODUCTION: "Ve výrobě",
    OrderStatus.READY: "Připraveno",
    OrderStatus.DELIVERED: "Dodáno",
    OrderStatus.CLOSED: "Uzavřeno",
    OrderStatus.CANCELLED: "Zrušeno",
}


def _safe_send(
    sender: EmailSender,
    kind: str,
    to: str,
    subject: str,
    html: str,
    text: str,
) -> None:
    """Send one email, logging and swallowing errors.

    Fire-and-forget: the caller is a FastAPI BackgroundTask running after
    the request has been served. Retry semantics arrive with the Dramatiq
    migration (roadmap R0).
    """
    try:
        sender.send(to=to, subject=subject, html=html, text=text)
        log.info("email.sent", kind=kind, to=to)
    except Exception as exc:
        log.error("email.failed", kind=kind, to=to, error=str(exc))


def send_invitation(
    sender: EmailSender,
    *,
    to: str,
    tenant_name: str,
    customer_name: str,
    contact_name: str,
    invite_url: str,
) -> None:
    """Send an invitation email to a new customer contact."""
    rendered = render_email(
        "invitation",
        {
            "tenant_name": tenant_name,
            "customer_name": customer_name,
            "contact_name": contact_name,
            "invite_url": invite_url,
        },
    )
    _safe_send(sender, "invitation", to, rendered.subject, rendered.html, rendered.text)


def send_staff_invitation(
    sender: EmailSender,
    *,
    to: str,
    tenant_name: str,
    invitee_name: str,
    invite_url: str,
) -> None:
    """Send an invitation email to a new tenant staff user."""
    rendered = render_email(
        "staff_invitation",
        {
            "tenant_name": tenant_name,
            "invitee_name": invitee_name,
            "invite_url": invite_url,
        },
    )
    _safe_send(sender, "staff_invitation", to, rendered.subject, rendered.html, rendered.text)


def send_password_reset(
    sender: EmailSender,
    *,
    to: str,
    tenant_name: str,
    full_name: str,
    reset_url: str,
) -> None:
    """Send a password-reset e-mail carrying a one-shot URL."""
    rendered = render_email(
        "password_reset",
        {
            "tenant_name": tenant_name,
            "full_name": full_name,
            "reset_url": reset_url,
        },
    )
    _safe_send(sender, "password_reset", to, rendered.subject, rendered.html, rendered.text)


def send_order_comment(
    sender: EmailSender,
    *,
    recipients: Iterable[str],
    tenant_name: str,
    order_number: str,
    order_title: str,
    order_url: str,
    author_name: str,
    body_excerpt: str,
) -> None:
    rendered = render_email(
        "order_comment",
        {
            "tenant_name": tenant_name,
            "order_number": order_number,
            "order_title": order_title,
            "order_url": order_url,
            "author_name": author_name,
            "body_excerpt": body_excerpt,
        },
    )
    for to in recipients:
        _safe_send(
            sender,
            "order_comment",
            to,
            rendered.subject,
            rendered.html,
            rendered.text,
        )


def send_order_submitted(
    sender: EmailSender,
    *,
    recipients: Iterable[str],
    tenant_name: str,
    customer_name: str,
    order_number: str,
    order_title: str,
    order_url: str,
) -> None:
    """Notify tenant staff that a customer just submitted an order."""
    rendered = render_email(
        "order_submitted",
        {
            "tenant_name": tenant_name,
            "customer_name": customer_name,
            "order_number": order_number,
            "order_title": order_title,
            "order_url": order_url,
        },
    )
    for to in recipients:
        _safe_send(
            sender,
            "order_submitted",
            to,
            rendered.subject,
            rendered.html,
            rendered.text,
        )


def send_order_status_changed(
    sender: EmailSender,
    *,
    recipients: Iterable[str],
    tenant_name: str,
    order_number: str,
    order_title: str,
    order_url: str,
    to_status: OrderStatus,
) -> None:
    """Notify customer contacts that an order's status changed."""
    label = STATUS_LABELS.get(to_status, to_status.value)
    rendered = render_email(
        "order_status_changed",
        {
            "tenant_name": tenant_name,
            "order_number": order_number,
            "order_title": order_title,
            "order_url": order_url,
            "status_label": label,
        },
    )
    for to in recipients:
        _safe_send(
            sender,
            "order_status_changed",
            to,
            rendered.subject,
            rendered.html,
            rendered.text,
        )
