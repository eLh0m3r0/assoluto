"""Public-demo guard (CEO decision E3).

Active **only** for the tenant named by ``PUBLIC_DEMO_TENANT`` and only
while that tenant carries the seed marker (``tenants.settings.demo_seed``)
— a real tenant can never be turned into a public playground by a typo.
For every other host the middleware does one string comparison and
passes the request through untouched.

What it does on the demo host:

* **No e-mail.** A context flag set for the request makes
  :func:`mail_suppressed` true, and ``app.tasks.email_tasks`` checks it
  before rendering / enqueueing anything. Mail built outside a request
  (weekly summaries, quote reminders, the outbox retry job) carries the
  tenant id and is matched against the demo tenant instead.
* **Blocked actions** (see :data:`_BLOCKED`) never reach their route:
  they 303 back to the page the visitor came from with
  ``?demo_blocked=<kind>``, which the demo banner turns into a friendly
  explanation. Blocked: password change / reset, profile (name, e-mail)
  changes, staff and contact invitations and re-sends, enabling /
  disabling / role changes of users and contacts, tenant settings,
  GDPR self-erase and self-export, customer archive, the full-data ZIP
  export, and anything under ``/platform/``. Allowed: everything that
  shows the product — orders, items, comments, status changes, quotes,
  confirmations, catalogue, material, POHODA / Money S3 / CSV / PDF
  downloads, and notification preferences (harmless: no mail leaves).
* **Logout** only clears the visitor's cookie. The normal logout bumps
  ``session_version``, which would sign out every other visitor sharing
  the demo login.
* ``/``, ``/auth/login`` lead to the ``/demo`` chooser.
* **Uploads** are capped by :func:`demo_upload_refusal` (images / PDF,
  2 MB, 20 new files a day for the tenant).
* ``X-Robots-Tag: noindex, nofollow`` on every response, and
  ``request.state.public_demo`` so ``base.html`` shows the banner.
"""

from __future__ import annotations

import re
import time
from contextvars import ContextVar
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import UUID

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings, get_settings
from app.demo import DEMO_MARKER
from app.logging import get_logger

log = get_logger("app.demo.guard")

#: Upload limits inside the public demo.
DEMO_MAX_UPLOAD_BYTES = 2 * 1024 * 1024
DEMO_MAX_UPLOADS_PER_DAY = 20

#: True while a request addressed to the public demo tenant is running —
#: background tasks scheduled by it inherit the value.
_DEMO_REQUEST: ContextVar[bool] = ContextVar("public_demo_request", default=False)

# slug -> (expires at monotonic, demo tenant id or None when refused)
_CACHE_TTL_SECONDS = 60.0
_cache: dict[str, tuple[float, UUID | None]] = {}


def configured_slug(settings: Settings) -> str:
    return (settings.public_demo_tenant or "").strip().lower()


def reset_cache() -> None:
    """Forget resolved demo tenants (tests; after the operator re-seeds)."""
    _cache.clear()


def _cached(slug: str) -> tuple[bool, UUID | None]:
    hit = _cache.get(slug)
    if hit is None or hit[0] < time.monotonic():
        return False, None
    return True, hit[1]


def _remember(slug: str, row: Any) -> UUID | None:
    tenant_id: UUID | None = None
    if row is not None:
        if (row.settings or {}).get(DEMO_MARKER):
            tenant_id = row.id
        else:
            log.warning(
                "public_demo.refused",
                slug=slug,
                reason="tenant was not created by the demo seed (no demo_seed marker)",
            )
    _cache[slug] = (time.monotonic() + _CACHE_TTL_SECONDS, tenant_id)
    return tenant_id


async def resolve_demo_tenant_id(settings: Settings) -> UUID | None:
    """Id of the public demo tenant, or None (off / missing / not a seed)."""
    slug = configured_slug(settings)
    if not slug:
        return None
    found, tenant_id = _cached(slug)
    if found:
        return tenant_id
    from sqlalchemy import select

    from app.db.session import get_sessionmaker
    from app.models.tenant import Tenant

    # ``tenants`` is not RLS-protected; the app role may read it.
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(Tenant.id, Tenant.settings).where(Tenant.slug == slug))
        ).one_or_none()
    return _remember(slug, row)


def resolve_demo_tenant_id_sync(settings: Settings) -> UUID | None:
    """Blocking twin of :func:`resolve_demo_tenant_id` for the mail path."""
    slug = configured_slug(settings)
    if not slug:
        return None
    found, tenant_id = _cached(slug)
    if found:
        return tenant_id
    from sqlalchemy import text

    from app.email.outbox import _sync_engine

    with _sync_engine().connect() as conn:
        row = conn.execute(
            text("SELECT id, settings FROM tenants WHERE slug = :slug"), {"slug": slug}
        ).one_or_none()
    return _remember(slug, row)


def mail_suppressed(tenant_id: UUID | str | None = None) -> bool:
    """True when a mail for ``tenant_id`` (or this request) must not go out.

    Called from the send / enqueue layer for every templated mail.
    """
    if _DEMO_REQUEST.get():
        return True
    if tenant_id is None:
        return False
    settings = get_settings()
    if not configured_slug(settings):
        return False
    try:
        demo_id = resolve_demo_tenant_id_sync(settings)
    except Exception as exc:  # pragma: no cover - DB down; the send fails anyway
        log.warning("public_demo.lookup_failed", error_class=type(exc).__name__)
        return False
    return demo_id is not None and str(demo_id) == str(tenant_id)


def is_public_demo_request(request: Request) -> bool:
    return bool(getattr(request.state, "public_demo", False))


def public_demo_signup_url(settings: Settings) -> str:
    """Where "Create your own portal" in the demo banner points (apex signup)."""
    return f"{settings.app_base_url.rstrip('/')}/platform/signup"


# ------------------------------------------------------------------ uploads

_MAGIC: dict[str, tuple[bytes, ...]] = {
    "application/pdf": (b"%PDF-",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/webp": (b"RIFF",),
}


def _looks_allowed(content_type: str, head: bytes) -> bool:
    ct = (content_type or "").lower().split(";", 1)[0].strip()
    prefixes = _MAGIC.get(ct)
    if not prefixes or not any(head.startswith(p) for p in prefixes):
        return False
    return ct != "image/webp" or head[8:12] == b"WEBP"


async def demo_upload_refusal(
    request: Request,
    db: Any,
    *,
    content_type: str,
    size_bytes: int,
    fileobj: Any,
) -> str | None:
    """Return a friendly refusal for an upload into the public demo, else None.

    Images (PNG, JPEG, WebP) and PDF only — checked against the file's
    first bytes, not just the browser's claim — up to 2 MB, at most 20
    new files a day for the whole tenant (counted from the upload audit
    trail, so deleting a file does not free a slot; the nightly reset
    does).
    """
    if not is_public_demo_request(request):
        return None
    from app.i18n import t as _t

    if size_bytes > DEMO_MAX_UPLOAD_BYTES:
        return _t(request, "The public demo accepts files up to 2 MB.")
    pos = fileobj.tell()
    head = fileobj.read(16)
    fileobj.seek(pos)
    if not _looks_allowed(content_type, head):
        return _t(request, "The public demo accepts only images (PNG, JPEG, WebP) and PDF files.")

    from datetime import UTC, datetime, timedelta

    from sqlalchemy import func, select

    from app.models.audit_event import AuditEvent

    since = datetime.now(UTC) - timedelta(days=1)
    uploads = (
        await db.execute(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == "attachment.upload", AuditEvent.created_at >= since)
        )
    ).scalar_one()
    if uploads >= DEMO_MAX_UPLOADS_PER_DAY:
        log.info("public_demo.upload_cap_reached", uploads=uploads)
        return _t(
            request,
            "The public demo accepts at most 20 new files a day. Please try again tomorrow.",
        )
    return None


# --------------------------------------------------------------- middleware

_ANY = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})
_READ = frozenset({"GET", "HEAD"})
_WRITE = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: (methods, path pattern, banner message kind, fallback when no Referer)
_BLOCKED: list[tuple[frozenset[str], re.Pattern[str], str, str]] = [
    # Accounts: passwords, profile (name / e-mail), GDPR self-service.
    (_ANY, re.compile(r"^/auth/password-reset(/confirm)?/?$"), "account", "/demo"),
    (_WRITE, re.compile(r"^/app/(admin|me)/profile/?$"), "account", "/app"),
    (_WRITE, re.compile(r"^/app/(admin|me)/profile/(password|delete)/?$"), "account", "/app"),
    (_READ, re.compile(r"^/app/(admin|me)/profile/export/?$"), "export", "/app"),
    # Staff users: invites, re-sends, enable / disable, role changes.
    (_WRITE, re.compile(r"^/app/admin/users/invite/?$"), "invite", "/app/admin/users"),
    (
        _WRITE,
        re.compile(r"^/app/admin/users/[^/]+/(disable|reactivate|edit|resend-invite)/?$"),
        "account",
        "/app/admin/users",
    ),
    # Customer contacts: invites (staff side and the client's own team page).
    (_WRITE, re.compile(r"^/app/customers/[^/]+/contacts/?$"), "invite", "/app/customers"),
    (
        _WRITE,
        re.compile(r"^/app/customers/[^/]+/contacts/[^/]+/(edit|disable|reactivate|resend-invite)/?$"),
        "account",
        "/app/customers",
    ),
    (_WRITE, re.compile(r"^/app/me/team/invite/?$"), "invite", "/app/me/team"),
    (_WRITE, re.compile(r"^/invite/(accept|staff)/?$"), "invite", "/demo"),
    # Customer archive locks every contact of that client out.
    (_WRITE, re.compile(r"^/app/customers/[^/]+/(archive|unarchive)/?$"), "customer", "/app/customers"),
    # Tenant settings and the whole-portal ZIP export.
    (_WRITE, re.compile(r"^/app/admin/tenant-settings/?$"), "settings", "/app"),
    (_READ, re.compile(r"^/app/admin/export/?$"), "export", "/app"),
    # Platform layer (billing, identity GDPR, tenant switching): not part of
    # the demo — not even to look at (P1-2: "Billing" used to open the
    # platform sign-in page inside the demo).
    (_ANY, re.compile(r"^/platform(/|$)"), "other", "/app"),
]  # fmt: skip

_UPLOAD_PATH = re.compile(r"^/app/orders/([0-9a-fA-F-]{36})/attachments/?$")
_TO_CHOOSER = frozenset({"/", "/auth/login", "/auth/login/"})


#: Path patterns of every blocked form submission (POST), for the page
#: script that shows those forms disabled up front (P3-13) instead of
#: letting the visitor fill one in only to be refused. Kept to the
#: regex subset JavaScript reads the same way (anchors, groups, classes).
LOCKED_FORM_PATTERNS: tuple[str, ...] = tuple(
    pattern.pattern for methods, pattern, _kind, _fallback in _BLOCKED if "POST" in methods
)


def blocked_rule(method: str, path: str) -> tuple[str, str] | None:
    """``(kind, fallback)`` when ``method path`` is blocked in the demo."""
    for methods, pattern, kind, fallback in _BLOCKED:
        if method in methods and pattern.match(path):
            return kind, fallback
    return None


def _header(scope: Scope, name: bytes) -> str:
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return ""


def _back_to(scope: Scope, current_path: str, fallback: str, method: str = "GET") -> str:
    """Same-origin path of the Referer (minus old flashes), else ``fallback``.

    A blocked *write* may go back to the very page that posted it (the
    settings form posting to itself) as long as reading that page is not
    blocked too — otherwise "Save" on the settings page used to bounce the
    visitor to the dashboard (P3-13). A blocked read never returns to
    itself, which would loop.
    """
    from app.routers.public import _safe_next_path

    referer = _header(scope, b"referer")
    target = fallback
    if referer:
        parts = urlsplit(referer)
        host = _header(scope, b"host")
        same_page_ok = parts.path != current_path or (
            method in _WRITE and blocked_rule("GET", parts.path) is None
        )
        if (not parts.netloc or parts.netloc == host) and same_page_ok:
            candidate = parts.path + (f"?{parts.query}" if parts.query else "")
            if _safe_next_path(candidate) == candidate:
                target = candidate
    return target


def _with_param(url: str, key: str, value: str) -> str:
    parts = urlsplit(url)
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in ("demo_blocked", "notice", "error")
    ]
    query.append((key, value))
    return f"{parts.path}?{urlencode(query)}"


async def _redirect(
    scope: Scope, receive: Receive, send: Send, url: str, *, clear_session: bool = False
) -> None:
    from starlette.responses import RedirectResponse

    response = RedirectResponse(url=url, status_code=303)
    if clear_session:
        from app.security.session import clear_session as _clear

        _clear(response)
    await response(scope, receive, send)


class PublicDemoMiddleware:
    """Pure-ASGI guard for the public demo host. See the module docstring."""

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def _is_demo(self, scope: Scope) -> bool:
        slug = configured_slug(self.settings)
        if not slug:
            return False
        from app.deps import resolve_tenant_slug

        if resolve_tenant_slug(Request(scope), self.settings) != slug:
            return False
        try:
            return await resolve_demo_tenant_id(self.settings) is not None
        except Exception as exc:  # DB trouble: the route will report it
            log.warning("public_demo.lookup_failed", error_class=type(exc).__name__)
            return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not await self._is_demo(scope):
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        state["public_demo"] = True
        state["public_demo_signup_url"] = public_demo_signup_url(self.settings)
        state["public_demo_locked"] = list(LOCKED_FORM_PATTERNS)
        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-robots-tag", b"noindex, nofollow"))
                if path.startswith("/demo"):
                    headers.append((b"cache-control", b"no-store, private"))
                message["headers"] = headers
            await send(message)

        token = _DEMO_REQUEST.set(True)
        try:
            await self._dispatch(scope, receive, send_wrapper, path, method)
        finally:
            _DEMO_REQUEST.reset(token)

    async def _dispatch(
        self, scope: Scope, receive: Receive, send: Send, path: str, method: str
    ) -> None:
        if method in _READ and path in _TO_CHOOSER:
            await _redirect(scope, receive, send, "/demo")
            return
        if method == "POST" and path.rstrip("/") == "/auth/logout":
            # Clear this visitor's cookie only — never bump the shared
            # login's session_version (that would sign everybody out).
            await _redirect(scope, receive, send, "/demo", clear_session=True)
            return

        rule = blocked_rule(method, path)
        if rule is not None:
            kind, fallback = rule
            log.info("public_demo.blocked", method=method, path=path, kind=kind)
            target = _with_param(_back_to(scope, path, fallback, method), "demo_blocked", kind)
            await _redirect(scope, receive, send, target)
            return

        upload = _UPLOAD_PATH.match(path) if method == "POST" else None
        if upload is not None:
            from app.security.body_limit import FORM_OVERHEAD_BYTES

            try:
                declared = int(_header(scope, b"content-length") or 0)
            except ValueError:
                declared = 0
            if declared > DEMO_MAX_UPLOAD_BYTES + FORM_OVERHEAD_BYTES:
                from app.i18n import t as _t

                message = _t(Request(scope), "The public demo accepts files up to 2 MB.")
                target = _with_param(f"/app/orders/{upload.group(1)}", "error", message)
                await _redirect(scope, receive, send, target)
                return

        await self.app(scope, receive, send)
