"""Pure-ASGI middleware that stamps security headers on every response.

Adds Content-Security-Policy (the main missing piece) plus a few other
belt-and-suspenders headers on top of whatever Caddy / uvicorn already
set. The CSP is intentionally strict: no inline scripts, no external
origins — the app only serves its own CSS/JS from ``/static``. If we
ever need to embed a third-party widget, we extend the policy.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# ``'unsafe-inline'`` on style-src is needed because ``base.html`` ships a
# tiny inline fallback stylesheet and product badges rely on Tailwind
# classes rendered inline. Drop when the inline <style> is removed.
_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "base-uri 'self'"
)

_EXTRA_HEADERS: list[tuple[bytes, bytes]] = [
    (b"content-security-policy", _CSP.encode("ascii")),
]


class SecurityHeadersMiddleware:
    """Append security headers to every HTTP response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {name for name, _ in headers}
                for name, value in _EXTRA_HEADERS:
                    if name not in existing:
                        headers.append((name, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)
