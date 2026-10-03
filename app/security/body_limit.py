"""Reject oversized request bodies before they are parsed.

FastAPI parses a multipart body completely (spooling it to a temp file)
before the endpoint — or any dependency — runs, so a size check inside
the upload handler only fires after the whole body has been received.
This pure-ASGI middleware enforces a hard ceiling while the body is
still streaming in (audit BE-17 / SEC-3):

* a declared ``Content-Length`` above the limit is answered with 413
  immediately, without reading a byte of the body;
* a body without a length (chunked) is counted as it arrives, and the
  read is aborted with 413 the moment it crosses the limit.

The ceiling is ``MAX_UPLOAD_SIZE_MB`` plus a small allowance for the
multipart framing and the other form fields; the precise per-file
limit is still checked by the upload handler.
"""

from __future__ import annotations

from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Multipart boundaries, the CSRF token and the other small fields.
FORM_OVERHEAD_BYTES = 1024 * 1024

_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in _BODY_METHODS:
            await self.app(scope, receive, send)
            return

        limit = self.max_body_bytes
        declared: int | None = None
        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = None
                break

        if declared is not None and declared > limit:
            await _send_413(send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # Starlette HTTPException is re-raised as-is by
                    # FastAPI's body parser and rendered as 413.
                    raise HTTPException(status_code=413, detail="Request body too large")
            return message

        await self.app(scope, limited_receive, send)


async def _send_413(send: Send) -> None:
    body = b'{"detail":"Request body too large"}'
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
