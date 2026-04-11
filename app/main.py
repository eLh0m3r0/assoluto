"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

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
from app.scheduler import build_scheduler
from app.storage.s3 import ensure_bucket_exists
from app.templating import Templates, build_jinja_env

STATIC_DIR = Path(__file__).resolve().parent / "static"


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
        title="SME Client Portal",
        version=__version__,
        debug=settings.app_debug,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.templates = Templates(build_jinja_env(), settings)
    app.state.email_sender = build_sender(settings)

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

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> Response:
        templates: Templates = request.app.state.templates
        # 401 on an HTML request → bounce to /auth/login instead of dumping
        # a raw JSON payload on the user.
        if exc.status_code == 401 and _wants_html(request):
            return RedirectResponse(url="/auth/login", status_code=status.HTTP_303_SEE_OTHER)
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
