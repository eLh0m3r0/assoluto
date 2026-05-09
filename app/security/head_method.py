"""Auto-derive HEAD from GET for every route.

FastAPI / Starlette only registers the verb you declare; HEAD never
falls back to GET. RFC 9110 requires HEAD wherever GET works, and
uptime monitors / link-checkers / security scanners frequently default
to HEAD as the cheap reachability probe — without this middleware
they all see ``HTTP 405 Allow: GET`` and report the site as down.

The middleware sits *inside* the response-modifying middlewares
(stays innermost) so the structured-log line still reports the request
as HEAD rather than the rewritten GET. We mutate ``scope["method"]``
to ``GET``, run the inner app, then drop every body chunk on the way
back so HEAD's "no body" contract holds. The Content-Length header
returned by the GET handler is left untouched — RFC 9110 §9.3.2 says
HEAD responses MAY echo the GET's Content-Length.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class HeadMethodMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "HEAD":
            await self.app(scope, receive, send)
            return

        # Re-dispatch as GET so routing finds the GET handler.
        rewritten: Scope = {**scope, "method": "GET"}

        async def head_send(message: Message) -> None:
            # Forward the start message verbatim — keeps headers (incl.
            # CSP and Set-Cookie attached by outer middlewares).
            if message["type"] == "http.response.start":
                await send(message)
                return
            if message["type"] == "http.response.body":
                # Strip body content; preserve more_body=False semantics.
                stripped: Message = {
                    "type": "http.response.body",
                    "body": b"",
                    "more_body": message.get("more_body", False),
                }
                await send(stripped)
                return
            # Anything else (websocket-style messages shouldn't appear
            # under http) passes through unchanged.
            await send(message)

        await self.app(rewritten, receive, head_send)
