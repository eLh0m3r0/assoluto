"""CSRF protection via the double-submit cookie pattern.

Architecture
------------
The work is split between a tiny ASGI middleware and a FastAPI
dependency so we don't run into BaseHTTPMiddleware pitfalls (reading
the request body in middleware breaks downstream form parsing).

1. **CsrfCookieMiddleware** (ASGI-level): inspects the incoming
   `csrftoken` cookie. If missing, it mints a fresh token and injects
   a `Set-Cookie` header into the outgoing response. It also stashes
   the token on `scope["state"]["csrf_token"]` so templates can read
   it via `request.state.csrf_token`.
2. **verify_csrf** (FastAPI dependency): attached to every router that
   accepts mutating requests. On non-safe methods it compares the
   cookie token against either the `X-CSRF-Token` header or the
   `csrf_token` form field. `await request.form()` here is safe —
   Starlette caches the parsed FormData and downstream `Form(...)`
   injections reuse it.

Templates include the token via a Jinja global `csrf_input()` which
renders a hidden input with the current value.
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import HTTPException, Request

CSRF_COOKIE_NAME = "csrftoken"
CSRF_FIELD_NAME = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"
COOKIE_MAX_AGE = 60 * 60 * 24 * 7  # 7 days

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def generate_csrf_token() -> str:
    """Return a URL-safe random token."""
    return secrets.token_urlsafe(32)


def tokens_match(a: str | None, b: str | None) -> bool:
    """Constant-time comparison of two CSRF token strings."""
    if not a or not b:
        return False
    return hmac.compare_digest(a, b)


# ---------------------------------------------------------- ASGI middleware


class CsrfCookieMiddleware:
    """Stamp a csrftoken cookie on the first response that lacks one.

    Unlike Starlette's BaseHTTPMiddleware this is a plain ASGI callable,
    so it does NOT wrap the request and the downstream handler sees the
    same request body. The middleware deliberately does not validate
    anything — validation lives in `verify_csrf` so it can read the
    form body via `await request.form()` safely.
    """

    def __init__(self, app) -> None:  # type: ignore[no-untyped-def]
        self.app = app

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        cookie_header = self._cookie_header(scope)
        existing = _parse_cookie(cookie_header, CSRF_COOKIE_NAME)
        issued = existing or generate_csrf_token()

        # Stash the token on request.state so templates can read it.
        state = scope.setdefault("state", {})
        state["csrf_token"] = issued

        needs_set_cookie = existing is None
        scheme_is_https = scope.get("scheme") == "https"

        async def send_wrapper(message):  # type: ignore[no-untyped-def]
            if needs_set_cookie and message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                secure_flag = "; Secure" if scheme_is_https else ""
                cookie_value = (
                    f"{CSRF_COOKIE_NAME}={issued}; Path=/; Max-Age={COOKIE_MAX_AGE}; "
                    f"SameSite=Lax{secure_flag}"
                )
                headers.append((b"set-cookie", cookie_value.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)

    @staticmethod
    def _cookie_header(scope) -> str:  # type: ignore[no-untyped-def]
        for name, value in scope.get("headers", []):
            if name == b"cookie":
                return value.decode("latin-1")
        return ""


def _parse_cookie(header: str, name: str) -> str | None:
    """Return the value of `name` in a raw `Cookie:` header, or None."""
    for part in header.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            if k == name:
                return v
    return None


# ------------------------------------------------------ FastAPI dependency


async def verify_csrf(request: Request) -> None:
    """Raise 403 if a mutating request is missing a matching CSRF token.

    Safe methods are no-ops. For POST/PUT/PATCH/DELETE we compare the
    cookie value against the `X-CSRF-Token` header, falling back to a
    `csrf_token` form field. `request.form()` is cached by Starlette so
    subsequent `Form(...)` dependencies in the endpoint see the same
    parsed body.
    """
    if request.method in SAFE_METHODS:
        return

    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    if not cookie_token:
        raise HTTPException(status_code=403, detail="CSRF cookie missing")

    header_token = request.headers.get(CSRF_HEADER_NAME)
    if header_token and tokens_match(cookie_token, header_token):
        return

    content_type = request.headers.get("content-type", "")
    if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        form = await request.form()
        submitted = form.get(CSRF_FIELD_NAME)
        if isinstance(submitted, str) and tokens_match(cookie_token, submitted):
            return

    raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
