"""Per-IP rate limiting for high-abuse public endpoints.

Why: the contact form, signup, verification-resend and login routes are
all reachable before any authentication and previously had no throttle.
A trivial loop could (a) turn the contact form into a mass-mail relay,
(b) brute-force passwords, (c) enumerate email addresses through the
"already registered" signup branch.

We use ``slowapi`` which wraps ``limits`` and plays nicely with FastAPI.

### Test mode

The limiter is globally disabled when ``APP_ENV=test`` — pytest runs the
same endpoint dozens of times per suite and we don't want to sprinkle
``limiter.reset()`` calls into every fixture. Production + development
keep the limiter on.
"""

from __future__ import annotations

from collections.abc import Callable

from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.requests import Request
from starlette.responses import JSONResponse

# Single process-wide limiter. Stateless in-memory store — fine for a
# single Uvicorn worker or a handful of them; swap to Redis-backed
# storage once we scale horizontally.
limiter: Limiter = Limiter(key_func=get_remote_address)


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Return 429 with a Retry-After hint. HTML callers get a plain text."""
    wants_html = "text/html" in request.headers.get("accept", "")
    detail = "Příliš mnoho pokusů, zkuste to prosím za chvíli."
    if wants_html:
        return JSONResponse(
            {"detail": detail},
            status_code=429,
            headers={"Retry-After": "60", "Content-Type": "text/plain; charset=utf-8"},
        )
    return JSONResponse({"detail": detail}, status_code=429, headers={"Retry-After": "60"})


def install(app, *, enabled: bool = True) -> None:
    """Attach the limiter + 429 handler to a FastAPI app.

    Call from ``create_app`` once. Safe to call with ``enabled=False`` in
    test mode — the decorators stay in place but do not rate-limit.
    """
    limiter.enabled = enabled
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)


# Human-friendly decorator aliases so routes don't have to import Limiter.
def limit(rule: str) -> Callable:
    """``@limit("5/15 minutes")`` — alias for ``limiter.limit(rule)``."""
    return limiter.limit(rule)
