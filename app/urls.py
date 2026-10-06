"""URL construction helpers."""

from __future__ import annotations

from urllib.parse import quote, urlparse

from app.config import Settings
from app.models.tenant import Tenant


def tenant_base_url(settings: Settings, tenant: Tenant) -> str:
    """Return the public base URL for tenant-scoped routes (e.g. invites,
    password reset, order links in emails).

    In subdomain-based multitenancy the tenant is resolved from the Host
    header, so links embedded in emails must point at the tenant's
    subdomain — otherwise they hit the apex and 404 with "Tenant not
    found".

    When `DEFAULT_TENANT_SLUG` is set (single-tenant self-host mode),
    tenant resolution falls back to the default slug and no subdomain
    is needed; we return `app_base_url` unchanged. Same for bare hosts
    like `localhost` or IPs where a subdomain can't be added.
    """
    return _base_url_for_slug(settings, tenant.slug)


def _base_url_for_slug(settings: Settings, slug: str) -> str:
    base = settings.app_base_url.rstrip("/")

    if settings.default_tenant_slug:
        return base

    parsed = urlparse(base)
    host = parsed.hostname or ""
    # Can't prepend a subdomain to an empty host, an IP, or bare names.
    if not host or host.replace(".", "").isdigit() or "." not in host:
        return base

    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{slug}.{host}{port}"


def public_demo_url(settings: Settings) -> str:
    """Entry page of the public demo (``https://<slug>.<apex>/demo``), or ``""``.

    Empty when ``PUBLIC_DEMO_TENANT`` is not set, which hides the "Try the
    live demo" buttons on the marketing site (E3).
    """
    slug = (settings.public_demo_tenant or "").strip().lower()
    if not slug:
        return ""
    return f"{_base_url_for_slug(settings, slug)}/demo"


def powered_by_url(settings: Settings, tenant: Tenant | None) -> str:
    """Link for the "Powered by Assoluto" footer, or ``""`` to hide it (MKT-9).

    Shown to *customer contacts* in their portal and in the emails they
    receive — the supplier's customers are often manufacturers with
    suppliers of their own. Points at the marketing site with
    ``?ref=portal&t=<tenant slug>`` so a resulting signup can be
    attributed (see ``app.platform.routers.signup._signup_ref_from_request``).

    Resolution: ``POWERED_BY_URL`` (``off`` disables), else ``https://<apex>``
    derived from ``PLATFORM_COOKIE_DOMAIN``, else nothing — a self-hosted
    install never advertises us by default. A tenant can opt out with
    ``tenants.settings["hide_powered_by"] = true`` (white-label).
    """
    if tenant is None:
        return ""
    if (getattr(tenant, "settings", None) or {}).get("hide_powered_by"):
        return ""
    base = (settings.powered_by_url or "").strip()
    if base.lower() == "off":
        return ""
    if not base:
        apex = (settings.platform_cookie_domain or "").strip().lstrip(".")
        if not apex or "." not in apex:
            return ""
        base = f"https://{apex}"
    sep = "&" if "?" in base else "?"
    if "?" not in base and not base.endswith("/"):
        base = base + "/"
    return f"{base}{sep}ref=portal&t={quote(tenant.slug, safe='')}"
