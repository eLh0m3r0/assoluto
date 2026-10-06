"""Marketing site tells the truth (audit 2026-10-03, theme T5).

Every claim removed in the truth pass gets a guard here so it can't
creep back: fabricated testimonials, "Most chosen", encryption at rest,
redundancy / 99.9 %, phone calls, custom domain, drawing revisions,
inconsistent support times, Backblaze, the phantom ``sme_theme`` cookie,
the dead EU ODR link. Plus the new surfaces: /security,
/.well-known/security.txt, the founding-customers block, the D3 price
list and its consistency with ``platform_plans``.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport

from app.email.sender import CaptureSender
from app.main import create_app
from tests.conftest import CsrfAwareClient

EN = {"Accept-Language": "en"}


def _with_operator(settings) -> None:
    settings.platform_operator_name = "Jan Provozovatel"
    settings.platform_operator_ico = "12345678"
    settings.platform_operator_address = "Masarykova 1, 405 02 Děčín"
    settings.platform_operator_email = "team@assoluto.eu"


@pytest.fixture
async def www(settings) -> AsyncIterator[CsrfAwareClient]:
    _with_operator(settings)
    app = create_app(settings)
    app.state.email_sender = CaptureSender()
    async with CsrfAwareClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac


@pytest.fixture
async def home(settings, wipe_db) -> AsyncIterator[CsrfAwareClient]:
    """Apex homepage needs FEATURE_PLATFORM and no default tenant."""
    from app.platform.deps import reset_platform_engine

    settings.feature_platform = True
    settings.default_tenant_slug = None
    _with_operator(settings)
    reset_platform_engine()
    app = create_app(settings)
    async with CsrfAwareClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac
    reset_platform_engine()


# Phrases that were false on 2026-10-03 and must not return.
FORBIDDEN_EVERYWHERE = (
    "encrypted at rest",
    "UptimeRobot",
    "redundant infrastructure",
    "99.9",
    "Most chosen",
    "What our first customers say",
    "Owner, metalwork shop",
    "Production manager, CNC machining",
    "Co-owner, sheet-metal fabrication",
    "call us",
    "portal.yourfirm.cz",
    "right revision",
    "(48 h)",
    "(12 h)",
    "same working day",
    "8-step",
    "managed hosting",
    "sme-client-portal",
    "tax document",
)


def _assert_clean(html: str) -> None:
    for phrase in FORBIDDEN_EVERYWHERE:
        assert phrase not in html, f"forbidden marketing claim back on the page: {phrase!r}"


def _jsonld(html: str) -> list[dict]:
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, flags=re.S)
    return [json.loads(b) for b in blocks]


# ------------------------------------------------------------------ homepage


@pytest.mark.postgres
async def test_homepage_has_no_fabricated_social_proof_and_shows_founding_programme(home) -> None:
    resp = await home.get("/", headers=EN)
    assert resp.status_code == 200
    html = resp.text
    _assert_clean(html)
    # D1 — founding customers instead of anonymous quotes.
    assert "Founding customers programme" in html
    assert "490 CZK / month for Starter, 1 490 CZK / month for Pro" in html
    # Positioning (MKT-7).
    assert "The customer portal for make-to-order manufacturers." in html
    assert "Next to your accounting software, not instead of it." in html
    assert "Customer-owned material" in html
    # About block uses the operator identity only.
    assert "Who is behind Assoluto" in html
    assert "Jan Provozovatel" in html and "12345678" in html


@pytest.mark.postgres
async def test_homepage_prices_and_jsonld_match_d3(home) -> None:
    html = (await home.get("/", headers=EN)).text
    assert "1&nbsp;490" in html and "2&nbsp;990" in html
    data = _jsonld(html)
    app = next(d for d in data if d["@type"] == "SoftwareApplication")
    prices = {o["name"]: o["price"] for o in app["offers"]}
    assert prices["Starter"] == "1490"
    assert prices["Pro"] == "2990"
    faq = next(d for d in data if d["@type"] == "FAQPage")
    # The FAQ JSON-LD is built from the same list as the visible FAQ.
    for entity in faq["mainEntity"]:
        assert entity["name"] in html
        assert "encrypted" not in entity["acceptedAnswer"]["text"]


@pytest.mark.postgres
async def test_homepage_hero_mock_fits_a_phone_and_shows_real_columns(home) -> None:
    html = (await home.get("/", headers=EN)).text
    # UX-02: single column below sm, sidebar layout only from sm up.
    assert "grid grid-cols-1 text-sm sm:grid-cols-[180px_1fr]" in html
    assert "grid grid-cols-[180px_1fr]" not in html
    # UX-03: no invented "Due" column / "need action" counter.
    mock = html.split("yourfirm.assoluto.eu/app/orders", 1)[1].split("The situation today", 1)[0]
    assert ">Due<" not in mock
    assert "need action" not in mock
    for col in ("Number", "Client", "Status"):
        assert f">{col}<" in mock


@pytest.mark.postgres
async def test_homepage_payment_answer_follows_stripe_switch(settings, home) -> None:
    html = (await home.get("/", headers=EN)).text
    # Stripe is off in tests (D2) — the page must not promise card checkout.
    assert "Online card payment is being set up" in html


# ------------------------------------------------------------------ pricing


async def test_pricing_page_shows_d3_prices_in_both_currencies(www) -> None:
    html = (await www.get("/pricing", headers=EN)).text
    _assert_clean(html)
    # EN visitors see EUR first, CZK underneath; annual = 2 months free.
    for fragment in ("59&nbsp;€", "119&nbsp;€", "1&nbsp;490", "2&nbsp;990"):
        assert fragment in html
    assert "14&nbsp;900&nbsp;Kč / 590&nbsp;€" in html
    assert "29&nbsp;900&nbsp;Kč / 1&nbsp;190&nbsp;€" in html
    assert "2 months free, invoiced" in html
    assert html.count("Unlimited client contacts") == 2
    assert "10 GB for drawings and attachments" in html
    assert "50 GB for drawings and attachments" in html
    # One support promise everywhere (UX-16 / BIZ-11).
    assert "Email support, reply within 1 working day" in html
    # Custom domain only on request (BIZ-18).
    assert "Custom domain on request" in html


async def test_pricing_page_czech_visitors_see_czk_first(www) -> None:
    html = (await www.get("/pricing", headers={"Accept-Language": "cs"})).text
    starter = html.split('data-plan="starter"', 1)[1].split('data-plan="pro"', 1)[0]
    assert starter.index("1&nbsp;490") < starter.index("59&nbsp;€")


async def test_pricing_page_puts_community_last(www) -> None:
    html = (await www.get("/pricing", headers=EN)).text
    order = re.findall(r'data-plan="(\w+)"', html)
    assert order == ["starter", "pro", "enterprise", "community"]


async def test_pricing_lists_pohoda_export_exactly_once(www) -> None:
    """Team POHODA ships the XML export in parallel; the claim lives in
    ONE removable line so it can be dropped if that work slips."""
    html = (await www.get("/pricing", headers=EN)).text
    assert html.count("Export of orders to Pohoda (XML)") == 1


@pytest.mark.postgres
async def test_pricing_page_matches_platform_plans_rows(www, owner_engine) -> None:
    """Marketing copy and the DB plan rows (migration 1011) must agree."""
    from sqlalchemy import text

    async with owner_engine.connect() as conn:
        rows = {
            r.code: r
            for r in (
                await conn.execute(
                    text(
                        "SELECT code, monthly_price_cents, max_users, max_contacts, "
                        "max_orders_per_month, max_storage_mb FROM platform_plans"
                    )
                )
            ).all()
        }
    html = (await www.get("/pricing", headers=EN)).text
    for code, card_end in (("starter", 'data-plan="pro"'), ("pro", 'data-plan="enterprise"')):
        row = rows[code]
        card = html.split(f'data-plan="{code}"', 1)[1].split(card_end, 1)[0]
        czk = f"{row.monthly_price_cents // 100:,}".replace(",", "&nbsp;")
        assert czk in card, (code, czk)
        assert f"{row.max_users} staff users" in card
        assert f"{row.max_storage_mb // 1024} GB" in card
        assert row.max_contacts is None and "Unlimited client contacts" in card
        assert row.max_orders_per_month is None and "Unlimited orders" in card


# ------------------------------------------------------------------ features


async def test_features_page_positioning_and_no_deep_security_detail(www) -> None:
    html = (await www.get("/features", headers=EN)).text
    _assert_clean(html)
    assert "Next to your accounting software, not instead of it" in html
    assert 'id="customer-material"' in html
    # Deep technical detail moved to /security (market.md §2).
    assert "Argon2" not in html
    assert 'href="/security"' in html
    assert "You quote the price and deadline" not in html
    assert "promised delivery date" in html
    assert "End-of-year inventory" not in html


# ------------------------------------------------------------------ security


async def test_security_page_lists_implemented_controls(www) -> None:
    resp = await www.get("/security", headers=EN)
    assert resp.status_code == 200
    html = resp.text
    _assert_clean(html)
    for fact in (
        "Row-Level Security",
        "Argon2id",
        "cross-site request forgery",
        "Hetzner Online GmbH",
        "backed up daily",
        "/.well-known/security.txt",
    ):
        assert fact in html, fact


async def test_security_txt_is_served_per_rfc_9116(www) -> None:
    resp = await www.get("/.well-known/security.txt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    assert "Contact: mailto:team@assoluto.eu" in body
    assert "Preferred-Languages: cs, en" in body
    expires = re.search(r"^Expires: (\S+)$", body, flags=re.M)
    assert expires is not None
    when = datetime.strptime(expires.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    days = (when - datetime.now(UTC)).days
    assert 360 <= days <= 366


async def test_sitemap_lists_security_page(www) -> None:
    body = (await www.get("/sitemap.xml")).text
    assert "/security</loc>" in body


async def test_footer_links_security_and_current_repo(www) -> None:
    html = (await www.get("/features", headers=EN)).text
    assert 'href="/security"' in html
    assert "https://github.com/eLh0m3r0/assoluto" in html


# ------------------------------------------------------------------ contact


async def test_contact_page_does_not_offer_phone_calls(www) -> None:
    html = (await www.get("/contact", headers=EN)).text
    _assert_clean(html)
    assert "call" not in html.lower().split("<main", 1)[-1].split("</main>", 1)[0]
    assert "Who is behind Assoluto" in html


# ------------------------------------------------------------------ legal


async def test_privacy_names_hetzner_storage_not_backblaze(www) -> None:
    html = (await www.get("/privacy", headers=EN)).text
    assert "Backblaze" not in html
    assert "Object Storage for uploaded files" in html
    assert "encrypted backups" not in html
    assert "Version 1.1" in html


async def test_cookies_page_lists_exactly_the_cookies_the_app_sets(www) -> None:
    # Platform cookie name — read from source text so the core test
    # doesn't import app.platform (CLAUDE.md §6 is about app code, but
    # keep the habit).
    from pathlib import Path

    from app.i18n import COOKIE_NAME as LOCALE_COOKIE
    from app.security.csrf import CSRF_COOKIE_NAME
    from app.security.session import SESSION_COOKIE_NAME

    platform_src = Path("app/platform/session.py").read_text()
    platform_cookie = re.search(r'PLATFORM_COOKIE_NAME = "([^"]+)"', platform_src).group(1)

    html = (await www.get("/cookies", headers=EN)).text
    listed = set(re.findall(r'<td class="px-3 py-2 font-mono text-xs">([^<]+)</td>', html))
    assert listed == {CSRF_COOKIE_NAME, SESSION_COOKIE_NAME, platform_cookie, LOCALE_COOKIE}
    assert "sme_theme" not in html
    assert "local storage" in html


async def test_terms_trial_runs_on_selected_plan_and_no_uptime_target(www) -> None:
    html = (await www.get("/terms", headers=EN)).text
    assert "trial of the plan selected at signup" in html
    assert "trial of the Starter plan" not in html
    assert "99.9" not in html
    assert "tax document" not in html
    # 1.2 (E1 early access) keeps the 1.1 changelog line below it.
    assert "Version 1.2" in html
    assert "Changes in 1.1" in html


async def test_imprint_drops_dead_odr_link(www) -> None:
    html = (await www.get("/imprint", headers=EN)).text
    assert "ec.europa.eu/consumers/odr" not in html
    assert "businesses only" in html


async def test_self_hosted_page_uses_overlay_command(www) -> None:
    html = (await www.get("/self-hosted", headers=EN)).text
    assert "-f docker-compose.yml -f docker-compose.prod.yml" in html
    assert "ready for nginx + TLS" not in html
    assert "4mex.localhost:8000" in html
