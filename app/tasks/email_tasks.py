"""Background email tasks.

Every public function here takes an `EmailSender` rather than reading a
global so that tests can plug in a `CaptureSender` without monkey-patching.
"""

from __future__ import annotations

from app.email.sender import EmailSender, render_email
from app.logging import get_logger

log = get_logger("app.tasks.email")


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
    try:
        sender.send(to=to, subject=rendered.subject, html=rendered.html, text=rendered.text)
        log.info("email.sent", kind="invitation", to=to)
    except Exception as exc:
        # Log-and-swallow: this is fire-and-forget from a request path.
        # Once we move to Dramatiq (R0) the task will retry automatically.
        log.error("email.failed", kind="invitation", to=to, error=str(exc))
