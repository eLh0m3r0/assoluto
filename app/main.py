"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.config import Settings, get_settings
from app.email.sender import build_sender
from app.logging import configure_logging, get_logger
from app.routers import assets as assets_router
from app.routers import attachments as attachments_router
from app.routers import customers as customers_router
from app.routers import dashboard as dashboard_router
from app.routers import health as health_router
from app.routers import orders as orders_router
from app.routers import products as products_router
from app.routers import public as public_router
from app.routers import tenant_admin as tenant_admin_router
from app.routers import www as www_router
from app.scheduler import build_scheduler
from app.security.csrf import CsrfCookieMiddleware
from app.security.headers import SecurityHeadersMiddleware
from app.security.locale import LocaleMiddleware
from app.storage.s3 import ensure_bucket_exists
from app.templating import Templates, build_jinja_env

STATIC_DIR = Path(__file__).resolve().parent / "static"


async def _normalize_demo_subscriptions(settings: Settings) -> None:
    """Flip any ``status='demo'`` subscriptions to ``trialing`` on boot.

    Called from the lifespan only when ``feature_platform`` + Stripe
    are both on. Demo-mode checkout stamped ``status='demo'`` locally
    but no Stripe webhook will ever transition those rows otherwise.

    Round-3 audit hardening:
    - Advisory lock (id 42_004) so multiple uvicorn workers racing
      this on simultaneous boot don't duplicate the UPDATE and trip
      over each other or over a just-arrived webhook.
    - Restrict to rows with a non-NULL ``trial_ends_at`` so we only
      upgrade genuine trial-flavoured demo rows; corrupt or
      mid-experiment rows with NULL trial remain as-is for manual
      inspection.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(settings.database_owner_url, pool_pre_ping=True)
    try:
        async with engine.begin() as conn:
            got_lock = (
                await conn.execute(
                    text("SELECT pg_try_advisory_lock(:id)"),
                    {"id": 42_004},
                )
            ).scalar()
            if not got_lock:
                # Another worker is already running the normaliser;
                # skip this worker's attempt.
                return
            try:
                await conn.execute(
                    text(
                        "UPDATE platform_subscriptions "
                        "SET status = 'trialing' "
                        "WHERE status = 'demo' "
                        "  AND trial_ends_at IS NOT NULL"
                    )
                )
            finally:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(:id)"),
                    {"id": 42_004},
                )
    finally:
        await engine.dispose()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """App lifespan hook — starts the in-process scheduler.

    The scheduler is skipped in test mode so pytest doesn't get unexpected
    background activity. Production and development both run it.
    """
    settings: Settings = app.state.settings
    log = get_logger("app.lifespan")
    log.info("app.starting", env=settings.app_env, version=__version__)

    scheduler = None
    if not settings.is_test:
        # Zero-setup MinIO: create the bucket on startup if it's missing.
        # Best-effort — a misconfigured S3 endpoint shouldn't block boot,
        # it just means uploads will fail loudly later.
        try:
            ensure_bucket_exists()
        except Exception as exc:
            log.warning("s3.bucket_init_failed", error=str(exc))

        # Normalise any lingering ``status='demo'`` rows to ``trialing``
        # when Stripe is now configured — an operator enabling Stripe
        # in production must not leave previous demo-mode subscriptions
        # in an unreachable state that no webhook can flip back.
        # Round-2 audit Backend-P2.
        if settings.feature_platform and settings.stripe_enabled:
            try:
                await _normalize_demo_subscriptions(settings)
            except Exception as exc:
                log.warning("billing.demo_cleanup_failed", error=str(exc))

        scheduler = build_scheduler()
        scheduler.start()
        app.state.scheduler = scheduler

    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        log.info("app.stopping")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="Assoluto",
        version=__version__,
        debug=settings.app_debug,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.templates = Templates(build_jinja_env(), settings)
    app.state.email_sender = build_sender(settings)

    # Per-IP rate limiting for public endpoints (contact, signup, verify
    # resend, login). Disabled during pytest (``APP_ENV=test``) so test
    # suites don't have to reset counters between cases.
    from app.security import rate_limit as _rate_limit

    _rate_limit.install(
        app,
        enabled=not settings.is_test,
        trusted_proxies=settings.trusted_proxies,
    )

    # Plain ASGI middleware that stamps the csrftoken cookie; validation
    # happens in `verify_csrf` as a router-level FastAPI dependency so it
    # can read the form body via `await request.form()` without breaking
    # downstream `Form(...)` injections.
    app.add_middleware(CsrfCookieMiddleware)

    # Adds Content-Security-Policy to every response. The other security
    # headers (HSTS, X-Frame-Options, etc.) come from Caddy / uvicorn.
    app.add_middleware(SecurityHeadersMiddleware)

    # Resolve the UI locale once per request (cookie -> Accept-Language ->
    # default). Result is available on ``request.state.locale`` and used
    # by the Jinja2 environment to pick the right gettext catalog.
    app.add_middleware(LocaleMiddleware, settings=settings)

    _mount_static(app)
    app.include_router(health_router.router)
    app.include_router(public_router.router)
    app.include_router(dashboard_router.router)
    app.include_router(customers_router.router)
    app.include_router(orders_router.router)
    app.include_router(attachments_router.router)
    app.include_router(products_router.router)
    app.include_router(assets_router.router)
    app.include_router(tenant_admin_router.router)
    app.include_router(www_router.router)

    # Fail fast if production deployment is misconfigured in a way that
    # silently gives free plans via the demo-mode checkout fallback.
    # Running platform in production WITHOUT Stripe means
    # /platform/billing/checkout/{plan} just flips the local
    # Subscription row to the chosen plan and redirects to success —
    # which is correct for dev but an expensive bug in prod.
    if settings.is_production and settings.feature_platform and not settings.stripe_enabled:
        raise RuntimeError(
            "FEATURE_PLATFORM is on in production but STRIPE_SECRET_KEY is "
            "empty. The billing checkout would silently grant paid plans. "
            "Set STRIPE_SECRET_KEY or turn FEATURE_PLATFORM off."
        )

    # Optional SaaS layer — loaded only when FEATURE_PLATFORM is on.
    # Core self-hosted builds skip this entirely so none of the
    # platform routes are reachable (the tables still exist in the
    # schema but are never touched).
    if settings.feature_platform:
        from app.platform import install as install_platform

        install_platform(app)

    _register_error_handlers(app)
    return app


def _register_error_handlers(app: FastAPI) -> None:
    """Render Jinja error pages for 404/403/500 on HTML clients.

    API/JSON clients continue to get the default JSON payload — we
    detect that via the `Accept` request header.
    """

    def _wants_html(request: Request) -> bool:
        accept = request.headers.get("accept", "")
        return "text/html" in accept or accept == ""

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
        templates: Templates = request.app.state.templates
        on_platform_path = request.url.path.startswith("/platform/")

        # 401 on an HTML request → bounce to the right login page,
        # preserving the original path as ?next= so the user lands
        # back where they wanted after authentication.
        if exc.status_code == 401 and _wants_html(request):
            login_url = "/platform/login" if on_platform_path else "/auth/login"
            from urllib.parse import quote

            from app.routers.public import _safe_next_path

            original = _safe_next_path(request.url.path)
            if original != "/" and original != login_url:
                login_url = f"{login_url}?next={quote(original, safe='/')}"
            return RedirectResponse(url=login_url, status_code=status.HTTP_303_SEE_OTHER)

        # 403 on a platform path that sets Location (i.e. ``require_verified_identity``)
        # should follow that hint rather than render a generic 403 that
        # would bounce the user back into the same loop.
        # Only same-origin paths are honoured — reject protocol-relative
        # ``//evil.com`` and backslash variants as an open-redirect guard
        # in case a future dev sets an absolute Location header
        # (round-3 Backend P3 defence-in-depth).
        if exc.status_code == 403 and _wants_html(request):
            location = (exc.headers or {}).get("Location")
            if (
                location
                and location.startswith("/")
                and not location.startswith("//")
                and "\\" not in location
            ):
                return RedirectResponse(url=location, status_code=status.HTTP_303_SEE_OTHER)

        if _wants_html(request) and exc.status_code in (403, 404):
            template = f"errors/{exc.status_code}.html"
            html = templates.render(request, template, {"principal": None})
            return HTMLResponse(html, status_code=exc.status_code)
        return JSONResponse(
            {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
        get_logger("app.errors").error(
            "unhandled", path=request.url.path, error=f"{type(exc).__name__}: {exc}"
        )
        templates: Templates = request.app.state.templates
        if _wants_html(request):
            html = templates.render(request, "errors/500.html", {"principal": None})
            return HTMLResponse(html, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return JSONResponse(
            {"detail": "Internal server error"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


def _mount_static(app: FastAPI) -> None:
    """Mount the `/static` folder for CSS, JS, images."""
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# Module-level app for `uvicorn app.main:app`
app = create_app()
