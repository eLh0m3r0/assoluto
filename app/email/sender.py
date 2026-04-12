"""SMTP email sender and Jinja2-based rendering of email templates.

Also ships a `CaptureSender` used by tests and by dev runs where you want
to inspect outbound mail without actually talking to an SMTP server.
"""

from __future__ import annotations

import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Protocol

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import Settings

EMAIL_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _email_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(EMAIL_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    html: str
    text: str


def render_email(template_name: str, context: dict[str, Any]) -> RenderedEmail:
    """Render `<name>.subject.txt`, `<name>.html`, and optionally `<name>.txt`.

    Missing `.txt` falls back to a stripped version of the HTML.
    """
    env = _email_env()
    subject = env.get_template(f"{template_name}.subject.txt").render(**context).strip()
    html = env.get_template(f"{template_name}.html").render(**context)

    try:
        text = env.get_template(f"{template_name}.txt").render(**context)
    except Exception:
        text = _html_to_text(html)

    return RenderedEmail(subject=subject, html=html, text=text)


def _html_to_text(html: str) -> str:
    """Dumb HTML -> text fallback; good enough for notification emails."""
    import re

    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class EmailSender(Protocol):
    def send(self, *, to: str, subject: str, html: str, text: str) -> None: ...


class SmtpSender:
    """Synchronous SMTP sender.

    Used from background tasks so the blocking network I/O doesn't land
    on the request path. Keeps dependencies minimal; if we outgrow this,
    swap in aiosmtplib at the call site.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def send(self, *, to: str, subject: str, html: str, text: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self._settings.smtp_from
        msg["To"] = to
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")

        with smtplib.SMTP(self._settings.smtp_host, self._settings.smtp_port, timeout=10) as client:
            if self._settings.smtp_starttls:
                client.starttls()
            if self._settings.smtp_user:
                client.login(self._settings.smtp_user, self._settings.smtp_password)
            client.send_message(msg)


@dataclass
class CapturedEmail:
    to: str
    subject: str
    html: str
    text: str


@dataclass
class CaptureSender:
    """Test/dev sender that records every message in memory."""

    outbox: list[CapturedEmail] = field(default_factory=list)

    def send(self, *, to: str, subject: str, html: str, text: str) -> None:
        self.outbox.append(CapturedEmail(to=to, subject=subject, html=html, text=text))


def build_sender(settings: Settings) -> EmailSender:
    """Return the right sender for the current environment."""
    if settings.app_env == "test":
        # Tests grab the sender off `app.state.email_sender` and assert on
        # its outbox. Returning a fresh CaptureSender here keeps the API
        # symmetric with production.
        return CaptureSender()
    return SmtpSender(settings)
