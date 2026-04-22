"""A tenant-A session cookie presented to tenant B must not cause
ERR_TOO_MANY_REDIRECTS.

Repro of the bug: a valid-signature session cookie from tenant A
decodes successfully on tenant B (both share APP_SECRET_KEY) but its
``tenant_id`` doesn't match tenant B's id. The public login / landing
pages used to treat "any decodable session" as "signed in" and
redirect to ``/app``, which then 401'd (mismatch), bounced back to
``/auth/login``, which saw the cookie again — loop.

Fix: ``read_session_for_tenant`` gates the cookie against the current
tenant. And we stamp a Set-Cookie deletion on the response so the
browser drops the zombie cookie on the next request.

These tests are unit-level — no Postgres needed. We exercise the
helpers directly plus mock out the FastAPI Request for the integration
path.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.security.session import (
    DEFAULT_MAX_AGE_SECONDS,
    SESSION_COOKIE_NAME,
    SessionData,
    _serializer,
    cookie_mismatches_tenant,
    read_session_for_tenant,
)

SECRET = "unit-test-secret-key-not-for-prod"


def _mint_cookie(tenant_id: str) -> str:
    data = SessionData(
        principal_type="user",
        principal_id=str(uuid4()),
        tenant_id=tenant_id,
        customer_id=None,
        mfa_passed=False,
        session_version=0,
    )
    return _serializer(SECRET).dumps(data.to_dict())


def _fake_request(cookie_value: str | None) -> SimpleNamespace:
    """A minimal Request stub exposing only ``cookies.get(name)``."""
    cookies: dict[str, str] = {}
    if cookie_value is not None:
        cookies[SESSION_COOKIE_NAME] = cookie_value
    return SimpleNamespace(cookies=cookies)


def test_no_cookie_returns_none() -> None:
    req = _fake_request(None)
    assert read_session_for_tenant(req, SECRET, str(uuid4())) is None
    assert cookie_mismatches_tenant(req, SECRET, str(uuid4())) is False


def test_valid_cookie_for_current_tenant_passes() -> None:
    tid = str(uuid4())
    req = _fake_request(_mint_cookie(tid))
    got = read_session_for_tenant(req, SECRET, tid)
    assert got is not None
    assert got.tenant_id == tid


def test_cookie_for_different_tenant_returns_none() -> None:
    """Core regression — cookie signed for A, checked on B."""
    tid_a = str(uuid4())
    tid_b = str(uuid4())
    req = _fake_request(_mint_cookie(tid_a))
    assert read_session_for_tenant(req, SECRET, tid_b) is None


def test_cross_tenant_cookie_flagged_for_deletion() -> None:
    """So the caller knows to stamp a Set-Cookie delete header."""
    tid_a = str(uuid4())
    tid_b = str(uuid4())
    req = _fake_request(_mint_cookie(tid_a))
    assert cookie_mismatches_tenant(req, SECRET, tid_b) is True


def test_tampered_cookie_flagged_for_deletion() -> None:
    """A cookie whose signature doesn't verify is also worth clearing."""
    tid = str(uuid4())
    # Append garbage so signature breaks.
    bad = _mint_cookie(tid) + "xxxxxx"
    req = _fake_request(bad)
    assert read_session_for_tenant(req, SECRET, tid) is None
    assert cookie_mismatches_tenant(req, SECRET, tid) is True


def test_valid_cookie_for_current_tenant_not_flagged() -> None:
    tid = str(uuid4())
    req = _fake_request(_mint_cookie(tid))
    assert cookie_mismatches_tenant(req, SECRET, tid) is False


def test_cookie_max_age_is_unchanged_by_helpers() -> None:
    """Sanity: the helpers don't silently shrink the cookie lifetime."""
    assert DEFAULT_MAX_AGE_SECONDS == 60 * 60 * 24 * 14
