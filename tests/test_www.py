"""Marketing (www) routes: features, pricing, self-hosted, contact, legal."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport

from app.email.sender import CaptureSender
from app.main import create_app
from tests.conftest import CsrfAwareClient


@pytest.fixture
async def www_client(settings) -> AsyncIterator[tuple[CsrfAwareClient, CaptureSender]]:
    """Plain app client with CaptureSender for the contact-form test."""
    app = create_app(settings)
    sender = CaptureSender()
    app.state.email_sender = sender
    transport = ASGITransport(app=app)
    async with CsrfAwareClient(transport=transport, base_url="http://testserver") as ac:
        yield ac, sender


async def test_features_page_renders(www_client) -> None:
    client, _ = www_client
    resp = await client.get("/features")
    assert resp.status_code == 200
    assert "What SME Portal can do" in resp.text


async def test_pricing_page_renders_all_tiers(www_client) -> None:
    client, _ = www_client
    resp = await client.get("/pricing")
    assert resp.status_code == 200
    for tier in ("Community", "Starter", "Pro", "Enterprise"):
        assert tier in resp.text


async def test_self_hosted_page_mentions_docker_and_agpl(www_client) -> None:
    client, _ = www_client
    resp = await client.get("/self-hosted")
    assert resp.status_code == 200
    assert "docker compose" in resp.text
    assert "AGPL" in resp.text


async def test_terms_page_404_when_operator_identity_missing(www_client) -> None:
    """Legal pages must not serve when the operator identity is not configured."""
    client, _ = www_client
    resp = await client.get("/terms")
    # Without PLATFORM_OPERATOR_* env vars set in the test fixture, the
    # route now 404s rather than publishing a half-filled ToS.
    assert resp.status_code == 404


async def test_privacy_page_404_when_operator_identity_missing(www_client) -> None:
    client, _ = www_client
    resp = await client.get("/privacy")
    assert resp.status_code == 404


async def test_terms_page_renders_with_operator_identity(settings) -> None:
    """When all operator ENV vars are filled, /terms and /privacy render
    the operator identity in place of the [placeholder] strings."""
    from httpx import ASGITransport

    from app.email.sender import CaptureSender
    from app.main import create_app

    settings.platform_operator_name = "ACME Provider s.r.o."
    settings.platform_operator_ico = "12345678"
    settings.platform_operator_address = "Masarykova 1, Praha"
    settings.platform_operator_email = "legal@acme-provider.cz"

    app = create_app(settings)
    app.state.email_sender = CaptureSender()
    transport = ASGITransport(app=app)
    async with CsrfAwareClient(transport=transport, base_url="http://testserver") as ac:
        resp = await ac.get("/terms")
        assert resp.status_code == 200
        assert "ACME Provider s.r.o." in resp.text
        assert "12345678" in resp.text
        # Placeholder strings from the old hardcoded template must be gone.
        assert "[doplnit" not in resp.text

        resp2 = await ac.get("/privacy")
        assert resp2.status_code == 200
        assert "ACME Provider s.r.o." in resp2.text
        assert "legal@acme-provider.cz" in resp2.text


def test_money_filter_czk_formats() -> None:
    from app.templating import _money_filter

    assert _money_filter(49000, "CZK") == "490 Kč"
    assert _money_filter(49050, "CZK") == "490,50 Kč"
    assert _money_filter(0, "CZK") == "0 Kč"
    assert _money_filter(None, "CZK") == "—"


def test_money_filter_eur_usd_and_fallback() -> None:
    from app.templating import _money_filter

    assert _money_filter(1990, "EUR") == "19,90 €"
    assert _money_filter(2500, "USD") == "25 $"
    # Unknown currency falls back to ISO code rendered after the amount.
    assert _money_filter(1234, "GBP") == "12,34 GBP"


async def test_contact_form_renders_empty(www_client) -> None:
    client, _ = www_client
    resp = await client.get("/contact")
    assert resp.status_code == 200
    assert "Write to us" in resp.text


async def test_contact_form_submission_sends_email(www_client) -> None:
    client, sender = www_client
    resp = await client.post(
        "/contact",
        data={
            "name": "Jan Novák",
            "email": "jan@example.com",
            "message": "Dobrý den, zajímá mě Pro plán.",
        },
    )
    assert resp.status_code == 200
    assert "Message sent" in resp.text
    # Background task ran by the TestClient in-process.
    assert len(sender.outbox) == 1
    assert "Jan Novák" in sender.outbox[0].text
    assert "jan@example.com" in sender.outbox[0].text


async def test_contact_form_escapes_html_injection(www_client) -> None:
    """Attacker-supplied HTML in name/message must not land live in the
    outbound email. Subject is header-encoded by EmailMessage; body must
    be manually escaped before f-string interpolation."""
    client, sender = www_client
    resp = await client.post(
        "/contact",
        data={
            "name": "<script>alert(1)</script>Evil",
            "email": "x@y.cz",
            "message": "Hello <a href='http://evil'>click</a>",
        },
    )
    assert resp.status_code == 200
    assert len(sender.outbox) == 1
    html_body = sender.outbox[0].html
    # The literal <script> / <a> tags must not appear — only the escaped
    # entity form is acceptable.
    assert "<script>" not in html_body
    assert "<a href='http://evil'>" not in html_body
    assert "&lt;script&gt;" in html_body
    assert "&lt;a href=" in html_body


async def test_contact_form_rejects_oversized_message(www_client) -> None:
    client, sender = www_client
    resp = await client.post(
        "/contact",
        data={
            "name": "X",
            "email": "x@y.cz",
            "message": "a" * 5000,
        },
    )
    assert resp.status_code == 400
    assert "znaků" in resp.text
    assert len(sender.outbox) == 0


async def test_contact_form_rejects_invalid_email(www_client) -> None:
    client, sender = www_client
    resp = await client.post(
        "/contact",
        data={"name": "X", "email": "not-an-email", "message": "Hi"},
    )
    assert resp.status_code == 400
    assert "e-mail" in resp.text.lower()
    assert len(sender.outbox) == 0


async def test_contact_form_rejects_empty_message(www_client) -> None:
    client, sender = www_client
    # Missing field → FastAPI returns 422 before our handler runs.
    resp = await client.post(
        "/contact",
        data={"name": "X", "email": "x@y.cz"},
    )
    assert resp.status_code == 422
    assert len(sender.outbox) == 0

    # Whitespace-only field passes FastAPI's required check but our
    # handler trims and returns 400.
    resp = await client.post(
        "/contact",
        data={"name": "X", "email": "x@y.cz", "message": "   "},
    )
    assert resp.status_code == 400
    assert "Vyplňte" in resp.text
    assert len(sender.outbox) == 0


@pytest.mark.postgres
async def test_landing_shows_marketing_when_platform_on_and_no_tenant(settings, wipe_db) -> None:
    settings.feature_platform = True
    settings.default_tenant_slug = None

    from app.platform.deps import reset_platform_engine

    reset_platform_engine()

    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with CsrfAwareClient(transport=transport, base_url="http://testserver") as ac:
        resp = await ac.get("/")
    assert resp.status_code == 200
    assert "Replace emails and phone calls with" in resp.text

    reset_platform_engine()
