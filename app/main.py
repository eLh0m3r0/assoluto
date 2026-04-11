"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
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
    return app


def _mount_static(app: FastAPI) -> None:
    """Mount the `/static` folder for CSS, JS, images."""
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# Module-level app for `uvicorn app.main:app`
app = create_app()
