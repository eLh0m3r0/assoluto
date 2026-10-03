"""Liveness / readiness probes.

Intentionally NOT behind tenant resolution — these must work even when
no tenant is configured (e.g. a container boot smoke test, or a load
balancer hitting the service before DNS is fully populated).

``/healthz`` is the cheap "is the process up?" probe used by orchestration
liveness — never touches downstream dependencies.

``/readyz`` confirms the app can actually serve traffic: it pings the
database and (unless ``READYZ_CHECK_S3=false``) the S3 bucket. The S3
probe uses a 2-second timeout and its result is cached for 30 seconds so
an uptime monitor hammering ``/readyz`` can't turn into an S3 request
storm, and a hung object store can't hang the probe. The deploy gate
keeps using the static ``/healthz`` on purpose — an S3 blip must not
roll back an otherwise healthy release.
"""

from __future__ import annotations

import time
from typing import Any

import anyio
from fastapi import APIRouter, Request, Response, status
from sqlalchemy import text

from app.db.session import get_engine

S3_PROBE_TIMEOUT_SECONDS = 2
S3_PROBE_CACHE_SECONDS = 30.0

# {"at": monotonic timestamp, "error": None | "<ErrorClass>"}
_s3_probe_cache: dict[str, Any] = {}


def _probe_s3_blocking() -> None:
    """``HeadBucket`` with a short-timeout client (blocking; run in a thread)."""
    import boto3
    from botocore.client import Config

    from app.config import get_settings

    settings = get_settings()
    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url or None,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        use_ssl=settings.s3_use_ssl,
        config=Config(
            signature_version="s3v4",
            connect_timeout=S3_PROBE_TIMEOUT_SECONDS,
            read_timeout=S3_PROBE_TIMEOUT_SECONDS,
            retries={"max_attempts": 1},
        ),
    )
    client.head_bucket(Bucket=settings.s3_bucket)


async def check_s3(now: float | None = None) -> str | None:
    """Return ``None`` when the bucket is reachable, else the error class.

    Cached for :data:`S3_PROBE_CACHE_SECONDS` (success and failure alike).
    """
    current = time.monotonic() if now is None else now
    cached_at = _s3_probe_cache.get("at")
    if cached_at is not None and current - cached_at < S3_PROBE_CACHE_SECONDS:
        return _s3_probe_cache.get("error")
    error: str | None = None
    try:
        with anyio.fail_after(S3_PROBE_TIMEOUT_SECONDS * 3):
            await anyio.to_thread.run_sync(_probe_s3_blocking, abandon_on_cancel=True)
    except Exception as exc:
        error = type(exc).__name__
    _s3_probe_cache["at"] = current
    _s3_probe_cache["error"] = error
    return error


def reset_s3_probe_cache() -> None:
    _s3_probe_cache.clear()


router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, str]:
    """Active readiness check — DB ping + S3 bucket probe. 503 on failure.

    The DB check uses the app's own (``portal_app``) engine — the same
    connection path real requests take — with a trivial ``SELECT 1`` that
    needs no tenant context, so this stays a pure infra probe.
    """
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "error", "detail": f"db_ping_failed: {type(exc).__name__}"}

    if request.app.state.settings.readyz_check_s3:
        s3_error = await check_s3()
        if s3_error is not None:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "error", "detail": f"s3_unreachable: {s3_error}"}
    return {"status": "ok"}
