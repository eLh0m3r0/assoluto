"""/dpa — Data Processing Agreement (GDPR Art. 28) and its links.

The subprocessor list is rendered from ``www/_subprocessors.html`` by both
/privacy and /dpa; these tests pin that the two pages agree and that
neither template grows its own copy again.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport

from app.email.sender import CaptureSender
from app.main import create_app
from tests.conftest import CsrfAwareClient

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates" / "www"
_ROW = re.compile(r'data-subprocessor="([^"]+)"')


@pytest.fixture
async def legal_client(settings) -> AsyncIterator[CsrfAwareClient]:
    settings.platform_operator_name = "ACME Provider s.r.o."
    settings.platform_operator_ico = "12345678"
    settings.platform_operator_address = "Masarykova 1, Praha"
    settings.platform_operator_email = "legal@acme-provider.cz"
    app = create_app(settings)
    app.state.email_sender = CaptureSender()
    async with CsrfAwareClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac


@pytest.mark.parametrize("lang", ["cs", "en", "de"])
async def test_dpa_renders_with_operator_identity(legal_client, lang) -> None:
    resp = await legal_client.get("/dpa", headers={"Accept-Language": lang})
    assert resp.status_code == 200
    body = resp.text
    assert "ACME Provider s.r.o." in body
    assert "12345678" in body
    assert "legal@acme-provider.cz" in body
    assert 'data-dpa-version="1.0"' in body
    # Print-to-PDF support: print stylesheet + the CSP-safe print button.
    assert "@media print" in body
    assert "data-print" in body
    assert "onclick" not in body
    # Links back to the documents it relies on.
    for href in ('href="/privacy"', 'href="/security"'):
        assert href in body


async def test_dpa_404_without_operator_identity(client) -> None:
    resp = await client.get("/dpa")
    assert resp.status_code == 404


async def test_dpa_subprocessors_match_privacy_page(legal_client) -> None:
    privacy = _ROW.findall((await legal_client.get("/privacy")).text)
    dpa = _ROW.findall((await legal_client.get("/dpa")).text)

    assert privacy, "privacy page lists no subprocessors"
    assert dpa, "DPA lists no subprocessors"
    # Same order, and every DPA subprocessor is disclosed in the privacy policy.
    assert dpa == [name for name in privacy if name in dpa]
    # Exactly the providers that process the Customer's (controller's) data:
    # hosting + storage + backups, and transactional e-mail. Payment and DNS
    # providers are on /privacy only.
    assert dpa == ["Hetzner Online GmbH", "Brevo (Sendinblue SAS)"]
    assert "Stripe Payments Europe Ltd." in privacy
    assert "Porkbun LLC" in privacy


def test_subprocessor_rows_live_in_one_template_only() -> None:
    """Single source of truth: no page carries its own table rows."""
    for page in ("privacy.html", "dpa.html"):
        source = (TEMPLATES / page).read_text(encoding="utf-8")
        assert "_subprocessors.html" in source, page
        assert "Hetzner Online GmbH</td>" not in source, page
        assert "Sendinblue" not in source, page


async def test_dpa_linked_from_footer_terms_privacy_security(legal_client) -> None:
    for path in ("/terms", "/privacy", "/security", "/features"):
        resp = await legal_client.get(path)
        assert resp.status_code == 200, path
        assert 'href="/dpa"' in resp.text, path


async def test_dpa_in_sitemap_with_operator_identity(legal_client) -> None:
    assert "/dpa</loc>" in (await legal_client.get("/sitemap.xml")).text


async def test_dpa_not_in_sitemap_without_operator_identity(client) -> None:
    assert "/dpa</loc>" not in (await client.get("/sitemap.xml")).text
