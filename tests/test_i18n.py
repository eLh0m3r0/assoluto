"""Tests for the i18n (Babel + gettext) plumbing."""

from __future__ import annotations

import pytest

from app.i18n import COOKIE_NAME, gettext, negotiate_locale, supported_locale_list


def test_gettext_translates_to_czech() -> None:
    # Ships with a compiled cs catalog; these msgids are in the nav.
    assert gettext("cs", "Orders") == "Objednávky"
    assert gettext("cs", "Assets") == "Majetek"
    assert gettext("cs", "Clients") == "Klienti"
    assert gettext("cs", "Draft") == "Koncept"
    assert gettext("cs", "Delivered") == "Dodáno"


def test_gettext_english_passes_through() -> None:
    # English catalog uses the msgid as the translation (identity).
    assert gettext("en", "Orders") == "Orders"
    assert gettext("en", "Clients") == "Clients"


def test_gettext_unknown_locale_returns_msgid() -> None:
    assert gettext("xx", "Orders") == "Orders"


def test_supported_locale_list_parses_comma_separated() -> None:
    assert supported_locale_list("cs,en") == ["cs", "en"]
    assert supported_locale_list("cs, en , ") == ["cs", "en"]
    # Dedup + order preservation.
    assert supported_locale_list("cs,cs,en") == ["cs", "en"]
    # Bogus falls back to default cs.
    assert supported_locale_list("") == ["cs"]
    assert supported_locale_list("not-a-locale") == ["cs"]


class _FakeRequest:
    """Minimal stand-in for negotiate_locale."""

    def __init__(self, cookies: dict | None = None, accept: str = "") -> None:
        self.cookies = cookies or {}
        self.headers = {"accept-language": accept}


@pytest.mark.parametrize(
    ("cookies", "accept", "expected"),
    [
        # Cookie wins.
        ({COOKIE_NAME: "en"}, "cs,cs-CZ", "en"),
        # Accept-Language primary match.
        ({}, "en-US,en;q=0.9", "en"),
        # Accept-Language language-only fallback.
        ({}, "cs-CZ,cs;q=0.9", "cs"),
        # Unknown -> default.
        ({}, "de,de-DE", "cs"),
        # No hints -> default.
        ({}, "", "cs"),
    ],
)
def test_negotiate_locale(cookies: dict, accept: str, expected: str) -> None:
    request = _FakeRequest(cookies=cookies, accept=accept)
    chosen = negotiate_locale(request, supported=["cs", "en"], default="cs")
    assert chosen == expected


@pytest.mark.postgres
async def test_set_lang_endpoint(tenant_client) -> None:
    """GET /set-lang sets the sme_locale cookie and redirects."""
    r = await tenant_client.get("/set-lang?lang=en&next=/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert "sme_locale=en" in r.headers.get("set-cookie", "")


@pytest.mark.postgres
async def test_set_lang_rejects_unsupported_locale(tenant_client) -> None:
    r = await tenant_client.get("/set-lang?lang=xx&next=/", follow_redirects=False)
    # Falls back to default locale; cookie still written.
    assert r.status_code == 303
    assert "sme_locale=cs" in r.headers.get("set-cookie", "")


@pytest.mark.postgres
async def test_set_lang_rejects_open_redirect(tenant_client) -> None:
    r = await tenant_client.get(
        "/set-lang?lang=en&next=https://evil.example.com/",
        follow_redirects=False,
    )
    assert r.status_code == 303
    # External URL was rejected; redirect goes to safe "/".
    assert r.headers["location"] == "/"
