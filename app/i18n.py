"""Internationalization (i18n) support.

This module loads compiled `.mo` files from ``app/locale/<lang>/LC_MESSAGES/``
and exposes helpers used by the Jinja2 environment, route handlers, and
background tasks.

Design notes:

* Convention: English strings are the message IDs (keys). Czech translations
  live in ``cs/LC_MESSAGES/messages.po``. English typically passes through
  unchanged, but we still ship an ``en/messages.po`` so translators have a
  file to edit.
* Locale is resolved per request from, in order:

  1. query param ``?lang=<code>`` (only when ``/set-lang`` endpoint sets it)
  2. cookie ``sme_locale``
  3. ``Accept-Language`` header (negotiated against ``supported_locales``)
  4. ``default_locale`` setting (``cs`` by default)

* Translations are cached in-process: a single ``Translations`` object per
  locale. No TTL — restart the app to pick up new ``.mo`` files. This is
  fine because compiled translations are baked into the Docker image.
"""

from __future__ import annotations

from pathlib import Path

from babel import Locale, UnknownLocaleError
from babel.support import NullTranslations, Translations
from fastapi import Request

LOCALE_DIR = Path(__file__).resolve().parent / "locale"
DOMAIN = "messages"
COOKIE_NAME = "sme_locale"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # one year

# Cached translations keyed by locale code.
_TRANSLATIONS_CACHE: dict[str, NullTranslations] = {}


def _load_translations(locale: str) -> NullTranslations:
    """Load (and cache) compiled gettext catalog for ``locale``."""
    cached = _TRANSLATIONS_CACHE.get(locale)
    if cached is not None:
        return cached
    try:
        translations = Translations.load(str(LOCALE_DIR), locales=[locale], domain=DOMAIN)
    except OSError:
        translations = NullTranslations()
    _TRANSLATIONS_CACHE[locale] = translations
    return translations


def reset_translations_cache() -> None:
    """Used by tests and ``pybabel compile`` watchers."""
    _TRANSLATIONS_CACHE.clear()


def supported_locale_list(raw: str) -> list[str]:
    """Parse the ``SUPPORTED_LOCALES`` env var (comma-separated codes).

    Returns a de-duplicated, order-preserving list. Falls back to
    ``["cs"]`` if nothing parseable is provided.
    """
    out: list[str] = []
    seen: set[str] = set()
    for part in (raw or "").split(","):
        code = part.strip().lower()
        if not code or code in seen:
            continue
        try:
            Locale.parse(code)
        except (UnknownLocaleError, ValueError):
            continue
        out.append(code)
        seen.add(code)
    return out or ["cs"]


def negotiate_locale(
    request: Request,
    supported: list[str],
    default: str,
) -> str:
    """Resolve the best locale for a request.

    Priority: cookie -> Accept-Language header -> default.
    """
    # Cookie wins (user picked it explicitly via language switcher).
    cookie_value = request.cookies.get(COOKIE_NAME)
    if cookie_value and cookie_value in supported:
        return cookie_value

    # Accept-Language header (e.g. ``cs-CZ,cs;q=0.9,en;q=0.8``).
    accept = request.headers.get("accept-language", "")
    for raw_entry in accept.split(","):
        entry = raw_entry.split(";")[0].strip().lower()
        if not entry:
            continue
        # Exact match first, then language-only match.
        if entry in supported:
            return entry
        primary = entry.split("-", 1)[0]
        if primary in supported:
            return primary

    return default if default in supported else supported[0]


def get_translations(locale: str) -> NullTranslations:
    """Public accessor; safe to call from any thread."""
    return _load_translations(locale)


def gettext(locale: str, message: str) -> str:
    """Translate ``message`` into ``locale``.

    Usage from Python code (where no request context is handy):

        from app.i18n import gettext
        text = gettext("en", "Account is disabled.")
    """
    return _load_translations(locale).gettext(message)


def t(request: Request, message: str) -> str:
    """Translate ``message`` using the request's resolved locale.

    Shortcut for router handlers that need to hand a localized error
    string to a template. Falls back to ``cs`` if the locale middleware
    never ran (pure unit tests without the middleware stack).
    """
    locale = getattr(getattr(request, "state", None), "locale", None) or "cs"
    return _load_translations(locale).gettext(message)


def ngettext(locale: str, singular: str, plural: str, n: int) -> str:
    """Plural-aware translation."""
    return _load_translations(locale).ngettext(singular, plural, n)


# A process-wide "identity" translator used as a fallback by Jinja when the
# environment is built before the locale is known. The real per-request
# catalog is installed via ``env.install_gettext_translations()`` on every
# render (see ``app/templating.py``). Use babel's ``NullTranslations``
# (subclass of stdlib gettext) so the static type matches the
# ``NullTranslations`` returned everywhere else in this module.
_IDENTITY = NullTranslations()


def identity_translations() -> NullTranslations:
    """Return a translations object that returns the input unchanged."""
    return _IDENTITY
