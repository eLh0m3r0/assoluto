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

import ipaddress
from collections.abc import Callable

from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from starlette.requests import Request
from starlette.responses import PlainTextResponse

# Single process-wide limiter. Stateless in-memory store — fine for a
# single Uvicorn worker or a handful of them; swap to Redis-backed
# storage once we scale horizontally (see app/config.py TRUSTED_PROXIES
# for the X-Forwarded-For handshake).
#
# Trusted-proxy list is mutated at ``install()`` time from settings.
_TRUSTED_PROXIES: list[ipaddress._BaseNetwork] = []


def _parse_trusted(raw: str) -> list[ipaddress._BaseNetwork]:
    networks: list[ipaddress._BaseNetwork] = []
    for entry in (raw or "").split(","):
        cidr = entry.strip()
        if not cidr:
            continue
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            continue
    return networks


def _client_ip(request: Request) -> str:
    """Return the real client IP, honouring X-Forwarded-For only when
    the direct peer is on the trusted-proxy allowlist.

    Without the allowlist an attacker can inject a forged
    ``X-Forwarded-For`` header and reset their rate-limit bucket at
    will. The slowapi default ``get_remote_address`` reads
    ``request.client.host`` which always yields the proxy IP behind
    Cloudflare / nginx — turning the per-IP limiter into one global
    bucket. Neither extreme is what we want; this helper picks the
    right balance.
    """
    peer = request.client.host if request.client else "127.0.0.1"
    if not _TRUSTED_PROXIES:
        return peer
    try:
        peer_addr = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not any(peer_addr in net for net in _TRUSTED_PROXIES):
        return peer
    xff = request.headers.get("x-forwarded-for", "").strip()
    if not xff:
        return peer
    # XFF is "client, proxy1, proxy2, ..." — the real client is the
    # left-most entry we encounter that is NOT itself trusted.
    for candidate in (c.strip() for c in xff.split(",")):
        try:
            addr = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if not any(addr in net for net in _TRUSTED_PROXIES):
            return candidate
    return peer


limiter: Limiter = Limiter(key_func=_client_ip)


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    """Return 429 with a Retry-After hint. HTML callers get plain text."""
    wants_html = "text/html" in request.headers.get("accept", "")
    detail = "Příliš mnoho pokusů, zkuste to prosím za chvíli."
    if wants_html:
        return PlainTextResponse(
            detail,
            status_code=429,
            headers={"Retry-After": "60"},
        )
    from starlette.responses import JSONResponse

    return JSONResponse({"detail": detail}, status_code=429, headers={"Retry-After": "60"})


def install(app, *, enabled: bool = True, trusted_proxies: str = "") -> None:
    """Attach the limiter + 429 handler to a FastAPI app.

    Call from ``create_app`` once. Safe to call with ``enabled=False`` in
    test mode — the decorators stay in place but do not rate-limit.

    ``trusted_proxies`` is a comma-separated list of CIDR blocks / IPs
    that are allowed to forward a real client IP in ``X-Forwarded-For``.
    Use the Cloudflare published ranges + your own nginx egress in
    production; keep empty in local dev.
    """
    global _TRUSTED_PROXIES
    _TRUSTED_PROXIES = _parse_trusted(trusted_proxies)
    limiter.enabled = enabled
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)


# Human-friendly decorator aliases so routes don't have to import Limiter.
def limit(rule: str) -> Callable:
    """``@limit("5/15 minutes")`` — alias for ``limiter.limit(rule)``."""
    return limiter.limit(rule)
