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


def test_templates_per_locale_env_is_cached_and_distinct() -> None:
    """Regression test for the thread-race hotfix: per-locale envs stay
    distinct instances, so ``install_gettext_translations`` is never
    re-called on a shared Environment under concurrent renders."""
    from app.config import get_settings
    from app.templating import Templates, build_jinja_env

    t = Templates(build_jinja_env(), get_settings())
    env_cs_1 = t._get_env_for_locale("cs")
    env_cs_2 = t._get_env_for_locale("cs")
    env_en = t._get_env_for_locale("en")

    # Same locale → same cached instance.
    assert env_cs_1 is env_cs_2
    # Different locales → different instances (that's the whole point).
    assert env_cs_1 is not env_en


@pytest.mark.postgres
async def test_set_lang_rejects_open_redirect(tenant_client) -> None:
    r = await tenant_client.get(
        "/set-lang?lang=en&next=https://evil.example.com/",
        follow_redirects=False,
    )
    assert r.status_code == 303
    # External URL was rejected; redirect goes to safe "/".
    assert r.headers["location"] == "/"


@pytest.mark.postgres
@pytest.mark.parametrize(
    "bad_next",
    [
        "//evil.example.com/path",  # protocol-relative URL
        "/\\evil.example.com",  # backslash-prefixed, some browsers fold to /
        "/\\\\evil.example.com",  # double backslash
        "https://evil.example.com/x",  # absolute URL
        "javascript:alert(1)",  # scheme injection
        "",  # empty
    ],
)
async def test_set_lang_rejects_every_known_open_redirect_vector(tenant_client, bad_next) -> None:
    """All of these must redirect to ``/`` and never off-site."""
    import urllib.parse

    encoded = urllib.parse.quote(bad_next, safe="")
    r = await tenant_client.get(
        f"/set-lang?lang=en&next={encoded}",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/", f"bad_next={bad_next!r} leaked"


@pytest.mark.postgres
async def test_signup_page_renders_in_english_when_locale_is_en(settings, wipe_db) -> None:
    """PR #8 regression test: the signup page was wrapped with gettext
    markers — confirm the English catalog actually delivers an English
    page when the locale cookie is ``en``."""
    from httpx import ASGITransport
    from sqlalchemy import text

    from app.main import create_app
    from tests.conftest import CsrfAwareClient

    settings.feature_platform = True

    from app.platform.deps import reset_platform_engine

    reset_platform_engine()
    # Ensure platform tables are empty for the fresh app.
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(settings.database_owner_url)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM platform_tenant_memberships"))
        await conn.execute(text("DELETE FROM platform_identities"))
    await engine.dispose()

    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with CsrfAwareClient(transport=transport, base_url="http://testserver") as ac:
        ac.cookies.set("sme_locale", "en")
        resp = await ac.get("/platform/signup")
    assert resp.status_code == 200
    assert '<html lang="en"' in resp.text
    # English copy from the en catalog.
    assert "Create your portal" in resp.text
    assert "30-day free trial" in resp.text
    # The Czech original must NOT be present.
    assert "Vytvořte si svůj portál" not in resp.text

    reset_platform_engine()


def test_safe_next_path_unit() -> None:
    """Unit test for the helper — fast feedback without httpx stack."""
    from app.routers.public import _safe_next_path

    assert _safe_next_path("/app") == "/app"
    assert _safe_next_path("/app?x=1") == "/app?x=1"
    assert _safe_next_path("/app/orders?status=draft") == "/app/orders?status=draft"
    # Open-redirect attacks collapse to "/".
    assert _safe_next_path("") == "/"
    assert _safe_next_path("//evil.com") == "/"
    assert _safe_next_path("/\\evil.com") == "/"
    assert _safe_next_path("https://evil.com") == "/"
    assert _safe_next_path("javascript:alert(1)") == "/"
    assert _safe_next_path("evil.com") == "/"  # no leading slash
    # Round-3 defence-in-depth: ``..`` in any encoding must not pass.
    assert _safe_next_path("/app/../admin") == "/"
    assert _safe_next_path("/app/%2e%2e/admin") == "/"
    assert _safe_next_path("/app/%2E%2E/admin") == "/"
    assert _safe_next_path("/%2e%2e") == "/"
