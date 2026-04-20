"""URL construction helpers."""

from __future__ import annotations

from urllib.parse import urlparse

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
    base = settings.app_base_url.rstrip("/")

    if settings.default_tenant_slug:
        return base

    parsed = urlparse(base)
    host = parsed.hostname or ""
    # Can't prepend a subdomain to an empty host, an IP, or bare names.
    if not host or host.replace(".", "").isdigit() or "." not in host:
        return base

    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{tenant.slug}.{host}{port}"
