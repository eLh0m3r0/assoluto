"""SMTP email sender and Jinja2-based rendering of email templates.

Also ships a :class:`CaptureSender` used by tests and dev runs where
you want to inspect outbound mail without actually talking to an SMTP
server.

### i18n

Email Jinja environments mirror the main-app pattern
(``app/templating.py``): one cached Environment per locale, each
constructed with ``jinja2.ext.i18n`` and ``install_gettext_translations``
called exactly once at construction time. Rendering is then a plain
template lookup with no per-render mutation.

Callers pass a resolved locale in via :func:`render_email`; see
``app.services.locale_service.resolve_email_locale`` for how the
locale is picked given a recipient / customer / tenant.
"""

from __future__ import annotations

import smtplib
import threading
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Protocol

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import Settings
from app.i18n import get_translations, identity_translations

EMAIL_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


_ENV_CACHE: dict[str, Environment] = {}
_ENV_LOCK = threading.Lock()


def _new_env(locale: str | None) -> Environment:
    """Build a Jinja env with translations baked in for ``locale``.

    ``locale=None`` installs the identity translator — msgids pass
    through unchanged. Useful for unit tests that only care about
    variable interpolation.
    """
    env = Environment(
        loader=FileSystemLoader(str(EMAIL_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
        extensions=["jinja2.ext.i18n"],
    )
    translations = identity_translations() if locale is None else get_translations(locale)
    env.install_gettext_translations(translations, newstyle=True)  # type: ignore[attr-defined]
    return env


def _get_env(locale: str | None) -> Environment:
    """Return a cached per-locale Environment, lazily constructing it."""
    key = locale or ""
    cached = _ENV_CACHE.get(key)
    if cached is not None:
        return cached
    with _ENV_LOCK:
        cached = _ENV_CACHE.get(key)
        if cached is None:
            cached = _new_env(locale)
            _ENV_CACHE[key] = cached
    return cached


def _reset_env_cache_for_tests() -> None:
    """Wipe the per-locale env cache — used by tests that reload catalogs."""
    with _ENV_LOCK:
        _ENV_CACHE.clear()


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    html: str
    text: str
    locale: str | None = None


def render_email(
    template_name: str,
    context: dict[str, Any],
    *,
    locale: str | None = None,
) -> RenderedEmail:
    """Render ``<name>.subject.txt``, ``<name>.html`` (and optionally
    ``<name>.txt``) into a :class:`RenderedEmail`.

    ``locale`` picks which compiled catalogue the embedded gettext
    calls resolve against. ``None`` means "no translation" — leaves
    msgids as-is. Missing ``.txt`` falls back to a stripped version
    of the HTML body.
    """
    env = _get_env(locale)
    # ``locale`` is exposed as a template variable so base/lang attributes
    # can reflect the render locale (``<html lang="{{ locale }}">``).
    render_ctx = {"locale": locale or "", **context}

    subject = env.get_template(f"{template_name}.subject.txt").render(**render_ctx).strip()
    html = env.get_template(f"{template_name}.html").render(**render_ctx)

    try:
        text = env.get_template(f"{template_name}.txt").render(**render_ctx)
    except Exception:
        text = _html_to_text(html)

    return RenderedEmail(subject=subject, html=html, text=text, locale=locale)


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
