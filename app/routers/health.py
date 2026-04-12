"""Liveness / readiness probes.

Intentionally NOT behind tenant resolution — these must work even when
no tenant is configured (e.g. a container boot smoke test, or a load
balancer hitting the service before DNS is fully populated).
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> dict[str, str]:
    # TODO: add a real DB + S3 ping once those dependencies are wired up.
    return {"status": "ok"}
