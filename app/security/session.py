"""Cookie-based signed sessions.

A minimal replacement for Starlette's SessionMiddleware that stores the
principal's identity in a signed cookie. We reach for our own thing
because we need tight control over the cookie name, flags, and
serialization format, and we want to read the cookie in sync code paths
(dependencies) without touching middleware state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from fastapi import Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

SESSION_COOKIE_NAME = "sme_portal_session"
DEFAULT_MAX_AGE_SECONDS = 60 * 60 * 24 * 14  # 14 days


PrincipalType = Literal["user", "contact"]


@dataclass(frozen=True)
class SessionData:
    """Everything the cookie needs to identify a logged-in principal.

    `session_version` is compared to the corresponding field on the user /
    contact row to support "invalidate all sessions on password change".
    """

    principal_type: PrincipalType
    principal_id: str
    tenant_id: str
    customer_id: str | None = None
    mfa_passed: bool = False
    session_version: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "t": self.principal_type,
            "p": self.principal_id,
            "tid": self.tenant_id,
            "cid": self.customer_id,
            "mfa": self.mfa_passed,
            "v": self.session_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionData:
        return cls(
            principal_type=data["t"],
            principal_id=data["p"],
            tenant_id=data["tid"],
            customer_id=data.get("cid"),
            mfa_passed=bool(data.get("mfa", False)),
            session_version=int(data.get("v", 0)),
        )


def _serializer(secret_key: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key=secret_key, salt="session-cookie")


def read_session(request: Request, secret_key: str) -> SessionData | None:
    """Decode the session cookie if present and valid, otherwise None.

    Does NOT check that the cookie's ``tenant_id`` matches the current
    tenant — callers on public pages (landing, login form, reset form)
    must additionally compare against the resolved tenant to avoid
    honouring a cookie that leaked across subdomains. Use
    :func:`read_session_for_tenant` when you have the tenant already.
    """
    raw = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw:
        return None
    try:
        payload = _serializer(secret_key).loads(raw, max_age=DEFAULT_MAX_AGE_SECONDS)
    except (SignatureExpired, BadSignature):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return SessionData.from_dict(payload)
    except (KeyError, TypeError, ValueError):
        return None


def read_session_for_tenant(
    request: Request, secret_key: str, tenant_id: str
) -> SessionData | None:
    """Return the session only if it belongs to ``tenant_id``.

    Prevents the "valid signature, wrong tenant" redirect loop where a
    public page sees ``read_session(...) is not None`` and bounces to
    ``/app``, which then 401s because the session's tenant_id doesn't
    match the resolved tenant, which bounces back to ``/auth/login``,
    which sees the cookie, which bounces to ``/app``, ad infinitum.

    Mismatched cookies should also be cleared on the response — see
    :func:`cookie_mismatches_tenant`.
    """
    data = read_session(request, secret_key)
    if data is None or data.tenant_id != tenant_id:
        return None
    return data


def cookie_mismatches_tenant(
    request: Request, secret_key: str, tenant_id: str
) -> bool:
    """True when the session cookie signature-validates but the
    embedded tenant_id is not ``tenant_id``.

    Callers should respond by stamping :func:`clear_session` on the
    outgoing response — otherwise the zombie cookie keeps triggering
    the tenant-mismatch path on every subsequent request.
    """
    raw = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw:
        return False
    data = read_session(request, secret_key)
    if data is None:
        # Cookie exists but signature is bad / expired — not a tenant
        # mismatch per se, but still worth clearing. Return True so the
        # caller clears it.
        return True
    return data.tenant_id != tenant_id


def write_session(
    response: Response,
    secret_key: str,
    data: SessionData,
    *,
    secure: bool,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> None:
    """Set the signed session cookie on `response`."""
    token = _serializer(secret_key).dumps(data.to_dict())
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session(response: Response) -> None:
    """Delete the session cookie on `response`."""
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
