"""Unit tests for tenant_base_url()."""

from __future__ import annotations

from types import SimpleNamespace

from app.urls import tenant_base_url


def _settings(base: str, default_slug: str = "") -> SimpleNamespace:
    return SimpleNamespace(app_base_url=base, default_tenant_slug=default_slug)


def _tenant(slug: str) -> SimpleNamespace:
    return SimpleNamespace(slug=slug)


def test_prepends_subdomain_on_public_domain() -> None:
    got = tenant_base_url(_settings("https://assoluto.eu"), _tenant("demo"))
    assert got == "https://demo.assoluto.eu"


def test_strips_trailing_slash() -> None:
    got = tenant_base_url(_settings("https://assoluto.eu/"), _tenant("acme"))
    assert got == "https://acme.assoluto.eu"


def test_preserves_port() -> None:
    got = tenant_base_url(_settings("https://assoluto.eu:8443"), _tenant("demo"))
    assert got == "https://demo.assoluto.eu:8443"


def test_default_tenant_slug_returns_base_unchanged() -> None:
    # Single-tenant self-host: no subdomain needed, tenant resolves via the
    # configured default slug.
    got = tenant_base_url(_settings("https://portal.acme.com", default_slug="acme"), _tenant("acme"))
    assert got == "https://portal.acme.com"


def test_localhost_returns_base_unchanged() -> None:
    got = tenant_base_url(_settings("http://localhost:8000"), _tenant("demo"))
    assert got == "http://localhost:8000"


def test_ip_address_returns_base_unchanged() -> None:
    got = tenant_base_url(_settings("http://127.0.0.1:8000"), _tenant("demo"))
    assert got == "http://127.0.0.1:8000"
