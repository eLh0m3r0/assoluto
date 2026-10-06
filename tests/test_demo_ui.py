"""Public demo — what the visitor sees (demo review 2026-10-06).

* P1-2  no billing / ``/platform/*`` links inside the demo, and the guard
        sends any ``/platform/*`` request back with the friendly flash;
* P2-14 a "Where to start" card on the supplier dashboard, and the
        customer persona lands on its open quote with a hint;
* P3-13 refused forms are marked for the page script that disables them,
        and a refused save returns to the page it came from.

The showcase rows are found by shape (most recent quote, oldest overdue
order in production, first material), never by seed numbers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from httpx import ASGITransport
from sqlalchemy import text

from app.demo.seed import DEMO_CONTACT_EMAIL, SeedResult, seed_demo
from app.main import create_app
from tests.conftest import CsrfAwareClient

pytestmark = pytest.mark.postgres

DEMO = "ukazka-ui"
OTHER = "dilna-ui"
PASSWORD = "Demo-heslo-1"


@pytest.fixture(autouse=True)
def _s3_env(monkeypatch):  # type: ignore[misc]
    monkeypatch.setenv("S3_ENDPOINT_URL", "")
    monkeypatch.setenv("S3_PUBLIC_ENDPOINT_URL", "")
    monkeypatch.setenv("S3_ACCESS_KEY", "test")
    monkeypatch.setenv("S3_SECRET_KEY", "test")
    monkeypatch.setenv("S3_BUCKET", "portal-demo-test")
    monkeypatch.setenv("S3_REGION", "eu-central-1")


@pytest.fixture
async def demo(settings, owner_engine, wipe_db) -> SeedResult:
    result = await seed_demo(slug=DEMO, password=PASSWORD, engine=owner_engine, files=False)
    settings.public_demo_tenant = DEMO
    # Billing links only render with the platform layer on — the case
    # the review hit in production.
    settings.feature_platform = True
    return result


async def _client(settings, slug: str) -> CsrfAwareClient:
    return CsrfAwareClient(
        transport=ASGITransport(app=create_app(settings)),
        base_url="http://testserver",
        headers={"X-Tenant-Slug": slug},
    )


@pytest.fixture
async def demo_client(settings, demo) -> AsyncIterator[CsrfAwareClient]:
    async with await _client(settings, DEMO) as client:
        yield client


async def _enter(client: CsrfAwareClient, role: str) -> str:
    resp = await client.post("/demo/enter", data={"role": role}, follow_redirects=False)
    assert resp.status_code == 303, resp.text[:300]
    return resp.headers["location"]


async def _scalar(owner_engine, sql: str, **params):
    async with owner_engine.connect() as conn:
        return (await conn.execute(text(sql), {"slug": DEMO, **params})).scalar()


async def _flagship_for_contact(owner_engine) -> UUID | None:
    return await _scalar(
        owner_engine,
        """
        SELECT o.id FROM orders o
        JOIN tenants t ON t.id = o.tenant_id
        JOIN customer_contacts c ON c.customer_id = o.customer_id AND c.tenant_id = t.id
        WHERE t.slug = :slug AND c.email = :email AND o.status = 'quoted'
        ORDER BY o.created_at DESC, o.number DESC LIMIT 1
        """,
        email=DEMO_CONTACT_EMAIL,
    )


# ------------------------------------------------------------------ P1-2


async def test_demo_pages_have_no_platform_links(demo_client) -> None:
    await _enter(demo_client, "staff")
    for path in ("/app", "/app/orders", "/app/admin/exports"):
        body = (await demo_client.get(path)).text
        assert 'href="/platform/' not in body, path
        assert "/platform/billing" not in body, path


async def test_platform_links_still_render_outside_the_demo(settings, demo, owner_engine) -> None:
    """Same build, ordinary tenant: the admin still reaches billing."""
    await seed_demo(slug=OTHER, password=PASSWORD, engine=owner_engine, files=False)
    async with await _client(settings, OTHER) as client:
        resp = await client.post(
            "/auth/login",
            data={"email": "vedouci@dilna-vzorova.example.com", "password": PASSWORD},
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text[:300]
        body = (await client.get("/app")).text
        assert 'href="/platform/billing"' in body
        assert "data-demo-locked" not in body
        assert "data-demo-start" not in body


@pytest.mark.parametrize("path", ["/platform/billing", "/platform/select-tenant", "/platform"])
async def test_guard_sends_platform_reads_back_with_a_flash(demo_client, path: str) -> None:
    await _enter(demo_client, "staff")
    resp = await demo_client.get(path, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app?demo_blocked=other"

    resp = await demo_client.get(
        path, headers={"Referer": "http://testserver/app/orders"}, follow_redirects=False
    )
    assert resp.headers["location"] == "/app/orders?demo_blocked=other"
    page = await demo_client.get(resp.headers["location"])
    assert page.status_code == 200
    assert 'role="alert"' in page.text


# ----------------------------------------------------------------- P3-13


async def test_refused_save_returns_to_the_form(demo_client) -> None:
    await _enter(demo_client, "staff")
    resp = await demo_client.post(
        "/app/admin/tenant-settings",
        data={"name": "Hacked"},
        headers={"Referer": "http://testserver/app/admin/tenant-settings"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app/admin/tenant-settings?demo_blocked=settings"


async def test_refused_read_never_loops_back_to_itself(demo_client) -> None:
    await _enter(demo_client, "staff")
    resp = await demo_client.get(
        "/app/admin/export",
        headers={"Referer": "http://testserver/app/admin/export"},
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/app?demo_blocked=export"


async def test_pages_mark_refused_forms_for_the_lock_script(demo_client) -> None:
    import json
    import re

    from app.demo.guard import LOCKED_FORM_PATTERNS, blocked_rule

    await _enter(demo_client, "staff")
    body = (await demo_client.get("/app/admin/tenant-settings")).text
    match = re.search(r"data-demo-locked='([^']+)'", body)
    assert match is not None
    patterns = json.loads(match.group(1))
    assert patterns == list(LOCKED_FORM_PATTERNS)
    assert any(re.match(p, "/app/admin/tenant-settings") for p in patterns)
    assert 'data-demo-locked-note="' in body
    # Every pattern the page gets is one the guard really refuses on POST,
    # and the allowed notification form is not among them.
    for path in ("/app/admin/tenant-settings", "/app/admin/users/invite"):
        assert blocked_rule("POST", path) is not None
    assert not any(re.match(p, "/app/admin/profile/notifications") for p in patterns)
    # JS reads them with ``new RegExp`` — keep to the shared regex subset.
    for p in patterns:
        assert "(?" not in p and "\\A" not in p and "\\Z" not in p


def test_lock_script_is_shipped() -> None:
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "app/static/js/app.js").read_text()
    assert "data-demo-locked" in js
    assert "data-dismissible" in js
    # The flash auto-fade must spare page banners (the demo banner used to
    # disappear 4 s after every page load).
    assert """[role="status"]:not([data-persistent])""" in js
    banner = (
        Path(__file__).resolve().parent.parent / "app/templates/_demo_banner.html"
    ).read_text()
    assert '<div role="status" data-persistent' in banner


# ----------------------------------------------------------------- P2-14


async def test_customer_lands_on_the_flagship_quote(demo_client, owner_engine) -> None:
    flagship = await _flagship_for_contact(owner_engine)
    assert flagship is not None, "the seed should give the customer persona an open quote"
    location = await _enter(demo_client, "customer")
    assert location == f"/app/orders/{flagship}"

    page = await demo_client.get(location)
    assert page.status_code == 200
    assert "data-demo-quote-hint" in page.text
    assert f"/app/orders/{flagship}/transitions/confirmed" in page.text


async def test_customer_without_a_quote_lands_on_the_dashboard(demo_client, owner_engine) -> None:
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE orders SET status = 'confirmed' WHERE status = 'quoted' AND tenant_id = "
                "(SELECT id FROM tenants WHERE slug = :slug)"
            ),
            {"slug": DEMO},
        )
    assert await _enter(demo_client, "customer") == "/app"


async def test_supplier_dashboard_says_where_to_start(demo_client, owner_engine) -> None:
    quote = await _scalar(
        owner_engine,
        "SELECT o.id FROM orders o JOIN tenants t ON t.id = o.tenant_id "
        "WHERE t.slug = :slug AND o.status = 'quoted' "
        "ORDER BY o.created_at DESC, o.number DESC LIMIT 1",
    )
    overdue = await _scalar(
        owner_engine,
        "SELECT o.id FROM orders o JOIN tenants t ON t.id = o.tenant_id "
        "WHERE t.slug = :slug AND o.status = 'in_production' "
        "AND o.promised_delivery_at < CURRENT_DATE "
        "ORDER BY o.promised_delivery_at, o.number LIMIT 1",
    )
    material = await _scalar(
        owner_engine,
        "SELECT a.id FROM assets a JOIN tenants t ON t.id = a.tenant_id "
        "WHERE t.slug = :slug AND a.is_active ORDER BY a.created_at, a.code LIMIT 1",
    )
    await _enter(demo_client, "staff")
    body = (await demo_client.get("/app")).text
    assert 'data-dismissible="demo-start"' in body
    assert f'href="/app/orders/{quote}" data-start="quote"' in body
    if overdue is not None:
        assert f'href="/app/orders/{overdue}" data-start="overdue"' in body
    assert f'href="/app/assets/{material}" data-start="material"' in body
    assert 'href="/app/admin/exports" data-start="exports"' in body
    assert "data-dismiss" in body


async def test_where_to_start_skips_missing_rows(demo_client, owner_engine) -> None:
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE orders SET status = 'delivered' WHERE status = 'in_production' "
                "AND tenant_id = (SELECT id FROM tenants WHERE slug = :slug)"
            ),
            {"slug": DEMO},
        )
    await _enter(demo_client, "staff")
    body = (await demo_client.get("/app")).text
    assert 'data-start="overdue"' not in body
    assert 'data-start="quote"' in body


async def test_customer_dashboard_has_no_supplier_card(demo_client) -> None:
    await _enter(demo_client, "customer")
    body = (await demo_client.get("/app")).text
    assert "data-demo-start" not in body


async def test_homepage_puts_the_demo_under_the_primary_cta(settings) -> None:
    settings.app_base_url = "https://assoluto.eu"
    settings.default_tenant_slug = None
    settings.feature_platform = True
    for configured in ("", "ukazka"):
        settings.public_demo_tenant = configured
        async with CsrfAwareClient(
            transport=ASGITransport(app=create_app(settings)), base_url="http://testserver"
        ) as client:
            body = (await client.get("/")).text
            nav = 'data-testid="nav-demo-link"' in body
            assert nav is bool(configured)
            if not configured:
                assert 'data-testid="public-demo-link"' not in body
                continue
            signup = body.index('href="/platform/signup"\n               class="group')
            hero_demo = body.index('data-testid="public-demo-link"')
            contact = body.index('href="/contact"\n               class="inline-flex w-full')
            # Directly under the primary CTA, before "Request a demo",
            # full width below ``sm``.
            assert signup < hero_demo < contact
            tag = body[body.rindex("<a", 0, hero_demo) : body.index(">", hero_demo)]
            assert "w-full" in tag and "sm:w-auto" in tag
            # Header "Try free" still goes to sign-up.
            assert 'href="/platform/signup" class="rounded-md bg-gradient-to-r' in body
