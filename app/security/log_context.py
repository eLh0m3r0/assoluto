"""Per-request log context middleware.

Binds ``request_id``, ``method``, ``path`` to structlog contextvars at
the start of every HTTP request and clears them at the end. Downstream
code that calls ``get_logger().info(...)`` automatically gets these
fields stamped onto the log line — no need to thread them as kwargs.

``tenant_id`` and ``principal_id`` are bound by ``get_db`` /
``get_current_principal`` later in the dependency chain (because they
require DB lookup); see ``app/deps.py``.

ASGI-native (not ``BaseHTTPMiddleware``) for the same reason as
``LocaleMiddleware``: never read or buffer the request body.
"""

from __future__ import annotations

from uuid import uuid4

import structlog
from starlette.types import ASGIApp, Receive, Scope, Send


class LogContextMiddleware:
    """Stamp request_id + method + path onto structlog context."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Trust an inbound X-Request-Id header (set by Caddy / a load
        # balancer) so cross-tier traces line up. Fall back to a fresh
        # uuid4 so every request still has a stable id.
        headers = dict(scope.get("headers") or [])
        rid = headers.get(b"x-request-id", b"").decode("ascii", errors="ignore").strip()
        if not rid:
            rid = uuid4().hex[:16]

        path = scope.get("path", "")
        method = scope.get("method", "")

        # ``clear_contextvars`` is the right cleanup — ``unbind_*`` would
        # leak whatever the previous request bound when the worker is
        # reused (uvicorn keepalive). Clear at start AND finally.
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=rid,
            method=method,
            path=path,
        )
        try:
            await self.app(scope, receive, send)
        finally:
            structlog.contextvars.clear_contextvars()
