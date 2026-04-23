"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

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
from app.routers import search as search_router
from app.routers import tenant_admin as tenant_admin_router
from app.routers import www as www_router
from app.scheduler import build_scheduler
from app.security.csrf import CsrfCookieMiddleware
from app.security.headers import SecurityHeadersMiddleware
from app.security.locale import LocaleMiddleware
from app.storage.s3 import ensure_bucket_exists
from app.templating import Templates, build_jinja_env

STATIC_DIR = Path(__file__).resolve().parent / "static"


async def _sync_stripe_prices_from_env(settings: Settings, log: Any) -> None:
    """UPSERT ``platform_plans.stripe_price_id`` from env for each plan.

    Rationale
    ---------
    Plans are seeded by the ``1003_billing`` migration with ``stripe_price_id
    = NULL`` because Stripe price IDs are per-environment (test vs. prod
    Stripe accounts rotate them). Without this boot-time sync, the
    ``create_checkout_session`` path silently no-ops — every upgrade
    attempt would bail out at the ``not plan.stripe_price_id`` check.

    Idempotent: if env is empty (Stripe not configured), no-op. If the
    DB already has the same price_id, no UPDATE fires. Safe under
    multi-worker boot because the UPSERT is atomic on a UNIQUE(code).
    """
    env_map = {
        "starter": (settings.stripe_price_starter or "").strip(),
        "pro": (settings.stripe_price_pro or "").strip(),
    }
    env_map = {code: pid for code, pid in env_map.items() if pid}
    if not env_map:
        return

    from sqlalchemy import select, text, update
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.platform.billing.models import Plan

    engine = create_async_engine(settings.database_owner_url, pool_pre_ping=True)
    try:
        async with engine.begin() as conn:
            # Advisory lock so concurrent worker boots don't race the
            # compare-and-update pair. Key chosen not to clash with
            # 42_004 used by the demo-subscription normaliser.
            got_lock = (
                await conn.execute(
                    text("SELECT pg_try_advisory_lock(:id)"), {"id": 42_005}
                )
            ).scalar()
            if not got_lock:
                return
            try:
                for code, price_id in env_map.items():
                    result = await conn.execute(
                        select(Plan.id, Plan.stripe_price_id).where(Plan.code == code)
                    )
                    row = result.one_or_none()
                    if row is None:
                        log.warning("stripe_price.sync.plan_missing", code=code)
                        continue
                    plan_id, current = row
                    if current != price_id:
                        await conn.execute(
                            update(Plan)
                            .where(Plan.id == plan_id)
                            .values(stripe_price_id=price_id)
                        )
                        log.info(
                            "stripe_price.sync.updated",
                            code=code,
                            price_id=price_id,
                        )
            finally:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(:id)"), {"id": 42_005}
                )
    finally:
        await engine.dispose()


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


def _warn_if_smtp_suspicious(settings: Settings, log: Any) -> None:
    """Flag SMTP configs that almost certainly won't deliver mail.

    This is a best-effort sanity check; it cannot catch every weird
    setup, but it catches the combos we've actually seen fail in
    production:

    * Real external host (not localhost / mailpit) on dev port 1025.
    * Credentials set but no TLS / STARTTLS — the provider will
      reject ``AUTH LOGIN`` in clear.
    * STARTTLS requested on port 465 (465 is implicit-TLS, STARTTLS
      belongs on 587).
    """
    host = (settings.smtp_host or "").strip().lower()
    port = settings.smtp_port
    has_auth = bool(settings.smtp_user)
    starttls = settings.smtp_starttls

    is_local = host in {"", "localhost", "127.0.0.1", "mailpit", "mailhog"}

    if not is_local and port == 1025:
        log.warning(
            "smtp.suspicious_config",
            reason="external_host_on_dev_port",
            host=host,
            port=port,
            hint="Brevo/Gmail/M365 use 587 STARTTLS or 465 TLS, not 1025",
        )
    if has_auth and not starttls and port not in (465,):
        log.warning(
            "smtp.suspicious_config",
            reason="auth_without_tls",
            host=host,
            port=port,
            hint="SMTP AUTH over cleartext — set SMTP_STARTTLS=true (port 587) or use port 465",
        )
    if starttls and port == 465:
        log.warning(
            "smtp.suspicious_config",
            reason="starttls_on_implicit_tls_port",
            host=host,
            port=port,
            hint="Port 465 uses implicit TLS. Set SMTP_STARTTLS=false for 465, or use port 587.",
        )


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

        # Pull Stripe price IDs from env into platform_plans so the
        # checkout flow has a price to hand to Stripe. Without this the
        # Starter / Pro plan rows retain stripe_price_id=NULL and every
        # upgrade click falls through the ``not plan.stripe_price_id``
        # guard in ``create_checkout_session`` — silent no-op.
        if settings.feature_platform:
            try:
                await _sync_stripe_prices_from_env(settings, log)
            except Exception as exc:
                log.warning("billing.stripe_price_sync_failed", error=str(exc))

        # SMTP sanity check — a misconfigured SMTP_PORT silently times out
        # every send and leaves no breadcrumb. We caught ourselves once with
        # prod pointing at Brevo (smtp-relay.brevo.com) on port 1025, which
        # Brevo doesn't even listen on; 31 mails in 24h timed out before the
        # next operator noticed.
        _warn_if_smtp_suspicious(settings, log)

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
    # Passing ``platform_cookie_domain`` (e.g. ``.assoluto.eu``) lets the
    # middleware widen ``form-action`` to allow cross-subdomain redirects
    # required by the /platform/switch handoff in multi-tenant setups.
    app.add_middleware(
        SecurityHeadersMiddleware,
        subdomain_apex=settings.platform_cookie_domain or None,
    )

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
    app.include_router(search_router.router)
    app.include_router(tenant_admin_router.router)
    app.include_router(www_router.router)

    # Fail fast if production deployment is misconfigured in a way that
    # silently gives free plans via the demo-mode checkout fallback.
    # Running platform in production WITHOUT Stripe means
    # /platform/billing/checkout/{plan} just flips the local
    # Subscription row to the chosen plan and redirects to success —
    # which is correct for dev but an expensive bug in prod.
    if settings.is_production and settings.feature_platform and not settings.stripe_enabled:
        import logging

        log = logging.getLogger("app.platform")
        if settings.feature_platform_allow_demo:
            log.warning(
                "FEATURE_PLATFORM=true in production WITHOUT Stripe; checkout "
                "runs in demo mode (FEATURE_PLATFORM_ALLOW_DEMO acknowledged). "
                "Plan changes flip the local Subscription row without charging."
            )
        else:
            # Loud warning instead of a hard fail so a mistyped env doesn't
            # brick the hosted demo stack. Real paying deployments will have
            # STRIPE_SECRET_KEY set; this branch is only reachable when
            # someone deliberately turns the platform on without billing.
            log.error("==================================================================")
            log.error("FEATURE_PLATFORM=true in production but STRIPE_SECRET_KEY is empty")
            log.error("AND FEATURE_PLATFORM_ALLOW_DEMO is not set.")
            log.error("Running anyway in DEMO mode — billing checkout flips local rows")
            log.error("without charging. This is NOT safe for real customers.")
            log.error("Add FEATURE_PLATFORM_ALLOW_DEMO=true to /etc/assoluto/env to")
            log.error("acknowledge this and silence the error-level log.")
            log.error("==================================================================")

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

    # Plan-limit overflows come from the service layer and map to 402
    # Payment Required with a friendly page + Upgrade CTA. Catching here
    # keeps the 4 enforcement call-sites silent (no try/except boilerplate
    # in every router).
    from app.platform.usage import PlanLimitExceeded

    @app.exception_handler(PlanLimitExceeded)
    async def plan_limit_handler(request: Request, exc: PlanLimitExceeded) -> Response:
        get_logger("app.billing").info(
            "plan_limit.exceeded",
            path=request.url.path,
            metric=exc.metric,
            limit=exc.limit,
            current=exc.current,
        )
        templates: Templates = request.app.state.templates
        if _wants_html(request):
            # Map the internal metric id to a localised human string so
            # the error page reads naturally.
            from app.i18n import t as _t

            metric_labels = {
                "users": _t(request, "staff users"),
                "contacts": _t(request, "client contacts"),
                "orders": _t(request, "orders this month"),
                "storage_mb": _t(request, "MB of storage"),
            }
            html = templates.render(
                request,
                "errors/plan_limit.html",
                {
                    "principal": None,
                    "metric_label": metric_labels.get(exc.metric, exc.metric),
                    "current": exc.current,
                    "limit": exc.limit,
                },
            )
            return HTMLResponse(html, status_code=status.HTTP_402_PAYMENT_REQUIRED)
        return JSONResponse(
            {
                "detail": "Plan limit exceeded",
                "metric": exc.metric,
                "limit": exc.limit,
                "current": exc.current,
            },
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
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
