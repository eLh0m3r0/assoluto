"""Pure-ASGI middleware that resolves the current request's UI locale.

Runs before routing and stores the result on ``scope["state"].locale`` (which
Starlette exposes as ``request.state.locale``). Templates read it via the
base context in :mod:`app.templating`.

The middleware is ASGI-native (not ``BaseHTTPMiddleware``) so it never reads
or buffers the request body — reading bodies in middleware has bitten us
before with CSRF (see ``app/security/csrf.py``).
"""

from __future__ import annotations

from typing import Any

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings
from app.i18n import negotiate_locale, supported_locale_list


class LocaleMiddleware:
    """Parse the preferred locale once per HTTP request and stash it."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self._default = settings.default_locale
        self._supported = supported_locale_list(settings.supported_locales)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Build a minimal Request-like facade just to reuse negotiate_locale.
        request = _ScopeRequest(scope)
        locale = negotiate_locale(request, self._supported, self._default)

        # Starlette sets up ``scope["state"]`` lazily; create the dict if needed.
        state: dict[str, Any] = scope.setdefault("state", {})
        state["locale"] = locale

        await self.app(scope, receive, send)


class _ScopeRequest:
    """Tiny facade exposing only what ``negotiate_locale`` needs.

    Creating a real ``starlette.requests.Request`` here would pull in the
    receive/send channels that we MUST NOT exhaust before the app reads them.
    """

    def __init__(self, scope: Scope) -> None:
        self._cookies: dict[str, str] | None = None
        self._scope = scope
        self._headers = Headers(scope=scope)

    @property
    def cookies(self) -> dict[str, str]:
        if self._cookies is None:
            raw = self._headers.get("cookie", "")
            # Reuse Starlette's parser without constructing a full Request.
            req = Request({"type": "http", "headers": [(b"cookie", raw.encode())]})
            self._cookies = req.cookies
        return self._cookies

    @property
    def headers(self) -> Headers:
        return self._headers


# Unused import guard for mypy / ruff when "Message" isn't referenced.
_ = Message
