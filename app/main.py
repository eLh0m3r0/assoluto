"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import Settings, get_settings
from app.logging import configure_logging, get_logger
from app.templating import Templates, build_jinja_env

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """App lifespan hook — place to start/stop scheduler, warm caches, etc."""
    settings: Settings = app.state.settings
    log = get_logger("app.lifespan")
    log.info("app.starting", env=settings.app_env, version=__version__)
    # NOTE: APScheduler start will be wired in later milestones (M6).
    try:
        yield
    finally:
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

    _mount_static(app)
    register_health_routes(app)
    register_root_route(app)
    return app


def _mount_static(app: FastAPI) -> None:
    """Mount the `/static` folder for CSS, JS, images."""
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def register_health_routes(app: FastAPI) -> None:
    """Register liveness/readiness probes."""

    @app.get("/healthz", tags=["health"])
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/readyz", tags=["health"])
    async def readyz() -> JSONResponse:
        # TODO(M0.5+): check DB connectivity, S3 reachability.
        return JSONResponse({"status": "ok"})


def register_root_route(app: FastAPI) -> None:
    """Register a minimal landing page at `/`."""

    @app.get("/", response_class=HTMLResponse, tags=["root"])
    async def root(request: Request) -> HTMLResponse:
        templates: Templates = request.app.state.templates
        html = templates.render(request, "index.html")
        return HTMLResponse(html)


# Module-level app for `uvicorn app.main:app`
app = create_app()
