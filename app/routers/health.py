"""Liveness / readiness probes.

Intentionally NOT behind tenant resolution — these must work even when
no tenant is configured (e.g. a container boot smoke test, or a load
balancer hitting the service before DNS is fully populated).

``/healthz`` is the cheap "is the process up?" probe used by orchestration
liveness — never touches downstream dependencies.

``/readyz`` confirms the app can actually serve traffic by pinging the
database. The deploy pipeline polls this after rolling the web container
so a startup with broken DB credentials / failed migration fails the
roll-out instead of silently shipping a container that 500s on every
real request.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.db.session import get_engine

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(response: Response) -> dict[str, str]:
    """Active readiness check — DB ping. Returns 503 on failure.

    The check uses the owner-DSN engine so it never depends on tenant
    context being set; this stays a pure infra probe. Owner DSN is also
    what migrations / scripts already use, so a green readyz means the
    same connection path the running app needs is alive.
    """
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "error", "detail": f"db_ping_failed: {type(exc).__name__}"}
    return {"status": "ok"}
