"""Jinja2 template setup.

We use `jinja2-fragments` so that HTMX endpoints can return a named fragment
from a full template without duplicating markup. Non-HTMX requests get the
full page; HTMX requests get only the requested block.

### Thread safety note

``jinja2.ext.i18n.install_gettext_translations`` is a **mutating** call on
the ``Environment``. Calling it per-request on a shared Environment — as
an earlier draft of this module did — races under concurrent requests and
can leak e.g. Czech translations into a render started for an English
client. To fix that we keep a **per-locale cache** of fully-configured
``Environment`` instances. Each instance has ``install_gettext_translations``
called exactly once at construction time and is never mutated after.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import Request
from jinja2 import Environment, FileSystemLoader, select_autoescape
from jinja2_fragments import render_block
from markupsafe import Markup

from app import __version__
from app.config import Settings
from app.i18n import get_translations, identity_translations

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


_CURRENCY_SYMBOLS = {
    "CZK": "Kč",
    "EUR": "€",
    "USD": "$",
}


def _money_filter(cents: Any, currency: str = "CZK") -> str:
    """Render a minor-unit amount as a human-friendly price.

    Examples:
        money_filter(49000, "CZK")  -> "490 Kč"
        money_filter(49050, "CZK")  -> "490,50 Kč"
        money_filter(1990, "EUR")   -> "19,90 €"
        money_filter(None, "CZK")   -> "—"

    Czech-style formatting: comma decimal separator, space thousands
    separator, currency symbol after the amount with a single space.
    Non-mapped currencies fall back to the ISO code.
    """
    if cents is None:
        return "—"
    try:
        amount_minor = int(cents)
    except (TypeError, ValueError):
        return "—"
    code = (currency or "CZK").upper()
    symbol = _CURRENCY_SYMBOLS.get(code, code)
    whole, fraction = divmod(abs(amount_minor), 100)
    # Czech thousands separator is a regular space (U+0020 is fine in HTML).
    whole_str = f"{whole:,}".replace(",", " ")
    sign = "-" if amount_minor < 0 else ""
    value = f"{sign}{whole_str},{fraction:02d}" if fraction else f"{sign}{whole_str}"
    return f"{value} {symbol}"


def _timeago_filter(value: Any) -> str:
    """Render a datetime as a short relative-time string.

    Examples:
        just now     (< 60 s)
        5m ago       (< 1 h)
        2h ago       (< 24 h)
        3d ago       (< 14 d)
        2w ago       (< 60 d)
        4mo ago      (< 365 d)
        2y ago       (else)

    The unit suffixes are intentionally short + locale-neutral so we
    don't have to thread translations through a filter; the dashboard
    i18n handles headings and action labels instead. ``None`` / non-
    datetime inputs render as an em-dash so mis-wired callers don't
    crash the page.
    """
    if value is None:
        return "—"
    if not isinstance(value, datetime):
        return str(value)
    # Treat naive datetimes as UTC (everything we persist is TIMESTAMPTZ).
    moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - moment
    seconds = int(delta.total_seconds())
    if seconds < 0:
        # Future timestamps shouldn't happen, but don't blow up.
        seconds = 0
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 14:
        return f"{days}d ago"
    if days < 60:
        return f"{days // 7}w ago"
    if days < 365:
        return f"{days // 30}mo ago"
    return f"{days // 365}y ago"


def _qty_filter(value: Any) -> str:
    """Render a Decimal/number without trailing zeros.

    Examples:
        Decimal('75.000') -> '75'
        Decimal('7.500')  -> '7.5'
        Decimal('0')      -> '0'
        None              -> ''
    """
    if value is None:
        return ""
    if isinstance(value, Decimal):
        # Normalize but guard against scientific notation that
        # Decimal.normalize() can produce for large integers.
        normalized = value.normalize()
        _sign, _digits, exponent = normalized.as_tuple()
        if isinstance(exponent, int) and exponent > 0:
            normalized = normalized.quantize(Decimal(1))
        text = format(normalized, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


def _new_environment(locale: str | None = None) -> Environment:
    """Construct a fresh Jinja2 Environment with translations installed.

    When ``locale`` is None the identity translator is used (msgid passes
    through unchanged). Otherwise the compiled ``.mo`` catalogue for the
    given locale is loaded and installed via ``jinja2.ext.i18n``.
    """
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "htm", "xml"]),
        enable_async=False,
        trim_blocks=True,
        lstrip_blocks=True,
        extensions=["jinja2.ext.i18n"],
    )
    translations = identity_translations() if locale is None else get_translations(locale)
    # ``install_gettext_translations`` is only mutating; after it returns
    # the Environment is effectively immutable for reads / renders as long
    # as nothing else calls it again on the same instance. This module
    # guarantees that — the per-locale cache returns the same instance
    # forever, and nobody outside this module holds a reference.
    env.install_gettext_translations(translations, newstyle=True)  # type: ignore[attr-defined]
    env.filters["qty"] = _qty_filter
    env.filters["money"] = _money_filter
    env.filters["timeago"] = _timeago_filter
    return env


def build_jinja_env() -> Environment:
    """Back-compat entry point for callers that want a single Environment.

    Returns an Environment wired up with the *identity* translator — good
    for template-validation / compile-time checks. Real per-request
    rendering goes through :class:`Templates` which picks a per-locale
    cached Environment.
    """
    return _new_environment(locale=None)


class Templates:
    """Thin wrapper exposing `render()` and `render_block()` with app context.

    Mirrors the ergonomics of Starlette's `Jinja2Templates` but goes through
    our own per-locale cached environments so translations never race
    between concurrent requests.
    """

    def __init__(self, env: Environment, settings: Settings) -> None:
        # ``env`` is kept for tests / introspection that reach inside; it
        # is the identity-translator instance returned by build_jinja_env()
        # and is used only when no locale is known.
        self.env = env
        self.settings = settings
        self._envs: dict[str, Environment] = {}
        self._envs_lock = threading.Lock()

    def _get_env_for_locale(self, locale: str) -> Environment:
        """Return the cached, immutable Environment for ``locale``.

        Builds + caches on first access; subsequent calls are a plain
        dict lookup. A lock protects first-time insertion from racing.
        """
        cached = self._envs.get(locale)
        if cached is not None:
            return cached
        with self._envs_lock:
            cached = self._envs.get(locale)
            if cached is None:
                cached = _new_environment(locale=locale)
                self._envs[locale] = cached
        return cached

    def _base_context(self, request: Request, extra: dict | None = None) -> dict:
        csrf_value = getattr(request.state, "csrf_token", "")

        def csrf_input() -> Markup:
            return Markup(f'<input type="hidden" name="csrf_token" value="{csrf_value}">')

        locale = getattr(request.state, "locale", self.settings.default_locale)

        # Detect an active platform session so templates can surface a
        # "switch portal" link when the visitor came in via /platform/login
        # or signup. Cheap — just a cookie read + HMAC verify, no DB hit.
        has_platform_session = False
        is_platform_admin = False
        if self.settings.feature_platform:
            try:
                from app.platform.session import read_platform_session

                sess = read_platform_session(request, self.settings.app_secret_key)
                if sess is not None:
                    has_platform_session = True
                    is_platform_admin = sess.is_platform_admin
            except Exception:
                # Defensive: a stale / malformed cookie must not break page
                # render. read_platform_session already swallows signature
                # errors; this is belt-and-braces.
                has_platform_session = False

        # Absolute URL to the platform-admin dashboard on the apex.
        # Rendering it inside a tenant subdomain would work (same web
        # app behind the wildcard Caddy), but we want the "⚙ Platform
        # admin" link in the avatar menu to visibly *leave* the tenant
        # portal and land on the operator surface. Falls back to a
        # relative path on single-host dev.
        platform_admin_url = "/platform/admin/dashboard"
        apex = (self.settings.platform_cookie_domain or "").strip().lstrip(".")
        if apex and "." in apex:
            platform_admin_url = f"https://{apex}/platform/admin/dashboard"

        context: dict = {
            "request": request,
            "app_version": __version__,
            "app_env": self.settings.app_env,
            "url_for": request.url_for,
            "csrf_token": csrf_value,
            "csrf_input": csrf_input,
            "locale": locale,
            "feature_platform": self.settings.feature_platform,
            "has_platform_session": has_platform_session,
            "is_platform_admin": is_platform_admin,
            "platform_admin_url": platform_admin_url,
        }
        if extra:
            context.update(extra)
        return context

    def _pick_env(self, request: Request) -> Environment:
        locale = getattr(request.state, "locale", self.settings.default_locale)
        return self._get_env_for_locale(locale)

    def render(
        self,
        request: Request,
        template_name: str,
        context: dict | None = None,
    ) -> str:
        """Render a full template to a string."""
        env = self._pick_env(request)
        template = env.get_template(template_name)
        return template.render(self._base_context(request, context))

    def render_block(
        self,
        request: Request,
        template_name: str,
        block_name: str,
        context: dict | None = None,
    ) -> str:
        """Render a single named block from a template (for HTMX fragments)."""
        env = self._pick_env(request)
        return render_block(
            env,
            template_name,
            block_name,
            **self._base_context(request, context),
        )
