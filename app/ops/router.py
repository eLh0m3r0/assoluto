"""``/healthz/backups`` — dead-man's switch for the nightly off-site backup.

The uptime workflow probes it next to ``/healthz``: a backup that silently
stopped (cron gone, credentials rotated, bucket full) turns the probe red
instead of being discovered on the day a restore is needed. The S3 listing
is cached so the endpoint costs nothing under repeated probing; the body
carries no detail beyond ok/stale.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Response, status
from starlette.concurrency import run_in_threadpool

from app.config import Settings, get_settings

router = APIRouter(tags=["health"])

_CACHE_SECONDS = 600
_cache: dict[str, tuple[float, str]] = {}


def _check(settings: Settings) -> str:
    from app.ops.offsite_backup import _clients_from_settings, latest_dump_age_hours

    _, _, target = _clients_from_settings()
    age = latest_dump_age_hours(target)
    if age is None or age > settings.backup_max_age_hours:
        return "stale"
    return "ok"


@router.get("/healthz/backups")
async def backups_health(
    response: Response, settings: Settings = Depends(get_settings)
) -> dict[str, str]:
    if not settings.backup_s3_bucket:
        # Unconfigured is fine for dev and self-hosters, never for production.
        if settings.app_env == "production":
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "unconfigured"}
        return {"status": "disabled"}

    cached = _cache.get("result")
    if cached is None or time.monotonic() - cached[0] > _CACHE_SECONDS:
        try:
            result = await run_in_threadpool(_check, settings)
        except Exception:
            result = "error"
        cached = (time.monotonic(), result)
        _cache["result"] = cached
    if cached[1] != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": cached[1]}
