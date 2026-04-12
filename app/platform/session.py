"""Platform-level signed cookie session.

A second cookie alongside the tenant-local `sme_portal_session`:
* Name: `sme_portal_platform`.
* Scope: parent domain (configurable via `PLATFORM_COOKIE_DOMAIN`) so
  every tenant subdomain shares the cookie.
* Contents: `identity_id`, optional `is_platform_admin` flag.

Tenant-local sessions still exist — the platform flow just makes it
convenient to bounce between tenants without re-entering a password.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

PLATFORM_COOKIE_NAME = "sme_portal_platform"
PLATFORM_MAX_AGE_SECONDS = 60 * 60 * 24 * 14  # 14 days


@dataclass(frozen=True)
class PlatformSession:
    identity_id: str
    is_platform_admin: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"iid": self.identity_id, "admin": self.is_platform_admin}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlatformSession:
        return cls(
            identity_id=str(data["iid"]),
            is_platform_admin=bool(data.get("admin", False)),
        )


def _serializer(secret_key: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key=secret_key, salt="platform-session")


def read_platform_session(request: Request, secret_key: str) -> PlatformSession | None:
    raw = request.cookies.get(PLATFORM_COOKIE_NAME)
    if not raw:
        return None
    try:
        payload = _serializer(secret_key).loads(raw, max_age=PLATFORM_MAX_AGE_SECONDS)
    except (SignatureExpired, BadSignature):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return PlatformSession.from_dict(payload)
    except (KeyError, TypeError, ValueError):
        return None


def write_platform_session(
    response: Response,
    secret_key: str,
    data: PlatformSession,
    *,
    domain: str | None,
    secure: bool,
) -> None:
    token = _serializer(secret_key).dumps(data.to_dict())
    response.set_cookie(
        key=PLATFORM_COOKIE_NAME,
        value=token,
        max_age=PLATFORM_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
        domain=domain or None,
    )


def clear_platform_session(response: Response, *, domain: str | None) -> None:
    response.delete_cookie(PLATFORM_COOKIE_NAME, path="/", domain=domain or None)
