"""Locale resolution for outbound email.

The portal is served in a mix of Czech (tenant default) and English
(international customers). An outbound email has exactly one reader,
so we have to pick *one* locale for it. The caller at the send site
knows who the recipient is; this module picks the locale.

### Resolution order

Priority is "most specific first":

    1. ``recipient.preferred_locale``        — user / contact set it themselves
    2. ``customer.preferred_locale``         — tenant admin set a customer-wide default
    3. ``tenant.settings["default_locale"]`` — tenant-wide default
    4. ``settings.default_locale``           — app-wide fallback (usually "cs")

Every step is optional. The chain must terminate at #4, which always
has a value. ``supported`` (the list of codes we actually have
compiled catalogs for) gates every step: if a row stores a locale we
no longer support (operator pruned it), we fall through rather than
render in a non-existent catalogue.

### Why no separate column on tenants?

Tenant admins flip this at most a handful of times during the portal's
lifetime. ``tenants.settings`` JSONB already houses this kind of
"once-per-tenant, low-traffic" config. Staying consistent there
avoids another schema migration + a fresh read path.
"""

from __future__ import annotations

from typing import Any, Protocol

from app.config import Settings


class _HasPreferredLocale(Protocol):
    preferred_locale: str | None


class _HasSettings(Protocol):
    settings: dict[str, Any]


def _normalise(raw: Any) -> str | None:
    """Coerce a cell to a short locale code ("cs", "en") or None."""
    if raw is None:
        return None
    code = str(raw).strip().lower()
    if not code:
        return None
    # The i18n module uses short codes; collapse "en-US" → "en" etc.
    return code.split("-", 1)[0]


def resolve_email_locale(
    *,
    recipient: _HasPreferredLocale | None = None,
    customer: _HasPreferredLocale | None = None,
    tenant: _HasSettings | None = None,
    settings: Settings,
    supported: list[str] | None = None,
) -> str:
    """Return the locale code to use for an outbound email.

    Every argument is optional except ``settings`` — the chain walks
    down the specificity tree, falling back until a supported locale
    is found.
    """
    if supported is None:
        from app.i18n import supported_locale_list

        supported = supported_locale_list(settings.supported_locales)

    # 1) Recipient override.
    code = _normalise(getattr(recipient, "preferred_locale", None))
    if code and code in supported:
        return code

    # 2) Customer-level default (applies to all contacts of that customer).
    code = _normalise(getattr(customer, "preferred_locale", None))
    if code and code in supported:
        return code

    # 3) Tenant-wide default (stored in tenants.settings JSONB).
    tenant_settings = getattr(tenant, "settings", None) or {}
    if isinstance(tenant_settings, dict):
        code = _normalise(tenant_settings.get("default_locale"))
        if code and code in supported:
            return code

    # 4) App-wide default.
    code = _normalise(settings.default_locale)
    if code and code in supported:
        return code
    # Absolute last resort — something is badly mis-configured, but we
    # must return *something* a compiled catalogue exists for.
    return supported[0] if supported else "cs"
