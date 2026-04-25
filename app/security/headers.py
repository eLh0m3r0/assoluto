"""Pure-ASGI middleware that stamps security headers on every response.

Adds Content-Security-Policy (the main missing piece) plus a few other
belt-and-suspenders headers on top of whatever Caddy / uvicorn already
set. The CSP is intentionally strict: no inline scripts, no external
origins — the app only serves its own CSS/JS from ``/static``. If we
ever need to embed a third-party widget, we extend the policy.

Multi-tenant subdomain note
---------------------------
``form-action`` applies to the full HTTP redirect chain following a form
submission. In a subdomain-per-tenant deployment the platform /switch
endpoint on the apex 303-redirects to ``{slug}.{apex}/platform/complete-switch``
for the tenant-session handoff; ``form-action 'self'`` blocks that
cross-origin redirect unless the apex's subdomains are explicitly
listed. The middleware therefore accepts an optional apex hostname and
adds ``https://*.{apex}`` to ``form-action``.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send


def _build_csp(subdomain_apex: str | None) -> bytes:
    """Compose the CSP string, adding a subdomain wildcard to ``form-action``
    when a platform apex (e.g. ``assoluto.eu``) is supplied.

    Everything else stays strict — script/style/default stays 'self' only,
    because subdomains serve the same app image from the same static prefix.
    """
    apex = (subdomain_apex or "").strip().lstrip(".")
    # Stripe Checkout: a 303 redirect from our checkout endpoint sends the
    # browser to https://checkout.stripe.com/... — Safari/Firefox enforce
    # form-action across the redirect chain and will block it without an
    # explicit allowance. Stripe's billing portal lives on the same hosts.
    stripe_sources = "https://checkout.stripe.com https://billing.stripe.com"
    form_action_sources = f"'self' {stripe_sources}"
    if apex and "." in apex:
        form_action_sources = f"'self' https://*.{apex} https://{apex} {stripe_sources}"

    # ``'unsafe-inline'`` on style-src is needed because ``base.html`` ships
    # a tiny inline fallback stylesheet and product badges rely on Tailwind
    # classes rendered inline. Drop when the inline <style> is removed.
    csp = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        f"form-action {form_action_sources}; "
        "base-uri 'self'"
    )
    return csp.encode("ascii")


class SecurityHeadersMiddleware:
    """Append security headers to every HTTP response.

    Pass ``subdomain_apex`` (e.g. the value of ``PORTAL_DOMAIN`` /
    ``PLATFORM_COOKIE_DOMAIN``) to allow cross-subdomain form-action
    redirects — required by the /platform/switch handoff.
    """

    def __init__(self, app: ASGIApp, *, subdomain_apex: str | None = None) -> None:
        self.app = app
        self._extra_headers: list[tuple[bytes, bytes]] = [
            (b"content-security-policy", _build_csp(subdomain_apex)),
        ]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {name for name, _ in headers}
                for name, value in self._extra_headers:
                    if name not in existing:
                        headers.append((name, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)
