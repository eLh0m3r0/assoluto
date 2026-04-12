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
    """Decode the session cookie if present and valid, otherwise None."""
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
