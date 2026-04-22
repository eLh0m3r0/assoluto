"""Time-limited signed tokens (invitations, magic links, password resets).

Built on top of `itsdangerous.URLSafeTimedSerializer`. Each token carries
a payload dict plus an issued-at timestamp that the verifier uses to
enforce a max age.

Why not JWT? We do not need cross-service verification or claims
standardisation; we want short, URL-safe, server-signed tokens that
cannot be forged without the secret and that expire. ItsDangerous fits
exactly.
"""

from __future__ import annotations

from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


class TokenError(Exception):
    """Raised when a token is invalid or expired."""


class InvalidToken(TokenError):
    """Raised when a token fails signature verification."""


class ExpiredToken(TokenError):
    """Raised when a token signature is valid but age exceeded max_age."""


# Single serializer instance per purpose; see `TokenPurpose` below for the
# canonical salts. Using distinct salts per purpose means a token issued
# for one flow cannot be replayed against another.
class TokenPurpose:
    INVITE = "customer-contact-invite"
    STAFF_INVITE = "tenant-staff-invite"
    MAGIC_LINK = "magic-link-login"
    PASSWORD_RESET = "password-reset"
    EMAIL_VERIFY = "platform-email-verify"
    PLATFORM_PASSWORD_RESET = "platform-password-reset"
    # One-shot handoff from /platform/switch/{slug} on the platform apex
    # to /platform/complete-switch on the target tenant subdomain. Carries
    # the membership id; verifier also re-checks the platform session so
    # a stolen token alone is useless. Valid for 60 s.
    PLATFORM_TENANT_HANDOFF = "platform-tenant-handoff"


def _serializer(secret_key: str, purpose: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key=secret_key, salt=purpose)


def create_token(secret_key: str, purpose: str, payload: dict[str, Any]) -> str:
    """Sign and return a URL-safe token carrying `payload`."""
    return _serializer(secret_key, purpose).dumps(payload)


def verify_token(
    secret_key: str,
    purpose: str,
    token: str,
    max_age_seconds: int,
) -> dict[str, Any]:
    """Verify and decode a token.

    Raises:
        ExpiredToken: signature is valid but `max_age_seconds` has passed.
        InvalidToken: signature verification failed.

    Returns the original payload dict.
    """
    try:
        data = _serializer(secret_key, purpose).loads(token, max_age=max_age_seconds)
    except SignatureExpired as exc:
        raise ExpiredToken(str(exc)) from exc
    except BadSignature as exc:
        raise InvalidToken(str(exc)) from exc

    if not isinstance(data, dict):
        raise InvalidToken("token payload is not a dict")
    return data
