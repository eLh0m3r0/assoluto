"""Optional SaaS / platform layer.

Everything that makes the portal a *multi-tenant hosted service* rather
than a single-tenant self-hosted app lives here: global Identity
accounts, cross-tenant memberships, platform admin UI for tenant
management, and the platform-level login flow.

Core (the `app/` package outside `app/platform`) NEVER imports from
this package. `app.main.create_app()` calls `install(app)` only when
`settings.feature_platform` is True, so an open-source self-hosted
build can simply ignore this directory (or delete it) without
breaking anything.

Licensing note: keeping this code in the same repo makes development
easier. If you ship the core under AGPL and the platform layer under
a different licence, split it out into its own Python package and
let hosted deployments install it as an extra.
"""

from __future__ import annotations

from fastapi import FastAPI


def install(app: FastAPI) -> None:
    """Register platform routes and middleware on `app`.

    Called from `app.main.create_app()` only when the FEATURE_PLATFORM
    flag is on. Safe to call multiple times — idempotent.
    """
    from app.platform.routers import platform_admin, platform_auth, signup

    # Register the platform routes with their own prefixes; they live
    # alongside the core tenant routes and share the same FastAPI app.
    app.include_router(platform_auth.router)
    app.include_router(platform_admin.router)
    app.include_router(signup.router)
