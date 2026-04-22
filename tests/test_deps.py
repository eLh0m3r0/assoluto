"""Unit tests for tenant resolution helpers in app.deps."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.config import Settings
from app.deps import _extract_subdomain, resolve_tenant_slug


@pytest.mark.parametrize(
    "host,expected",
    [
        ("4mex.portal.example.com", "4mex"),
        ("4mex.localhost", "4mex"),
        ("4mex.localhost:8000", "4mex"),
        ("localhost", None),
        ("localhost:8000", None),
        ("127.0.0.1", None),
        ("127.0.0.1:8000", None),
        ("", None),
        ("www.portal.example.com", None),
        # Registered domain (SLD.TLD) is the apex — no subdomain.
        # Previously this returned ``example`` which made every apex
        # hit 404 with "tenant not found"; now apex requests fall
        # through to the www/platform landing.
        ("example.com", None),
        ("assoluto.eu", None),
        # 3+ labels imply a real subdomain.
        ("demo.assoluto.eu", "demo"),
        ("test-a.assoluto.eu", "test-a"),
    ],
)
def test_extract_subdomain(host: str, expected: str | None) -> None:
    assert _extract_subdomain(host) == expected


def _make_request(host: str = "", x_tenant_slug: str | None = None) -> MagicMock:
    req = MagicMock()
    headers = {}
    if host:
        headers["host"] = host
    if x_tenant_slug is not None:
        headers["x-tenant-slug"] = x_tenant_slug
    req.headers.get = lambda key, default="": headers.get(key.lower(), default)
    return req


def test_header_takes_precedence_over_subdomain() -> None:
    settings = Settings(DEFAULT_TENANT_SLUG=None)
    req = _make_request(host="beta.portal.example.com", x_tenant_slug="alpha")
    assert resolve_tenant_slug(req, settings) == "alpha"


def test_subdomain_is_used_when_no_header() -> None:
    settings = Settings(DEFAULT_TENANT_SLUG=None)
    req = _make_request(host="4mex.portal.example.com")
    assert resolve_tenant_slug(req, settings) == "4mex"


def test_default_slug_is_fallback() -> None:
    settings = Settings(DEFAULT_TENANT_SLUG="self")
    req = _make_request(host="localhost:8000")
    assert resolve_tenant_slug(req, settings) == "self"


def test_returns_none_when_nothing_resolves() -> None:
    settings = Settings(DEFAULT_TENANT_SLUG=None)
    req = _make_request(host="localhost:8000")
    assert resolve_tenant_slug(req, settings) is None


def test_slug_is_lowercased() -> None:
    settings = Settings(DEFAULT_TENANT_SLUG="SelfHost")
    req = _make_request(host="localhost")
    assert resolve_tenant_slug(req, settings) == "selfhost"
