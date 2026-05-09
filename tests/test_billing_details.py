"""Tests for the IČO/DIČ billing-details form + the verify-email gate
on the platform tenant-handoff routes.

Covers F-BE-007 (verify-gate on select-tenant / switch / complete-switch)
and F-BE-008 (billing-details validation, audit row, gate redirect).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.email.sender import CaptureSender
from app.main import create_app
from app.models.tenant import Tenant
from tests.conftest import CsrfAwareClient
from tests.test_billing import _mark_verified

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Local copy of the ``billing_client`` fixture
# ---------------------------------------------------------------------------
#
# pytest's fixture discovery only walks conftest.py files, so a fixture
# defined in another test module isn't visible here. Duplicating the
# 25-line fixture is the lightest fix; promoting it to conftest.py is
# overkill for two callers.


@pytest.fixture
async def billing_client(
    settings, wipe_db, owner_engine
) -> AsyncIterator[tuple[CsrfAwareClient, CaptureSender]]:
    settings.feature_platform = True
    settings.stripe_secret_key = ""

    async with owner_engine.begin() as conn:
        await conn.execute(text("DELETE FROM platform_tenant_memberships"))
        await conn.execute(text("DELETE FROM platform_identities"))
        await conn.execute(text("DELETE FROM platform_subscriptions"))
        await conn.execute(text("DELETE FROM platform_invoices"))

    from app.platform.deps import reset_platform_engine

    reset_platform_engine()

    app = create_app(settings)
    sender = CaptureSender()
    app.state.email_sender = sender
    transport = ASGITransport(app=app)
    async with CsrfAwareClient(transport=transport, base_url="http://testserver") as ac:
        yield ac, sender

    reset_platform_engine()


# ---------------------------------------------------------------------------
# Shared signup helper
# ---------------------------------------------------------------------------


async def _signup_and_get_tenant(
    billing_client,
    *,
    email: str,
    company_name: str = "TenantCo",
    slug: str = "tenantco",
):
    """POST /platform/signup, returning the just-created tenant row.

    The signup leaves the Identity unverified by design. Callers that
    need a verified identity call ``_mark_verified(owner_engine, email)``
    afterwards.
    """
    client, _ = billing_client
    resp = await client.post(
        "/platform/signup",
        data={
            "company_name": company_name,
            "slug": slug,
            "owner_email": email,
            "owner_full_name": "Owner",
            "password": "correct-horse-battery-staple",
            "terms_accepted": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


# ---------------------------------------------------------------------------
# F-BE-007: verify-email gate on platform handoff routes
# ---------------------------------------------------------------------------


HTML_HEADERS = {"Accept": "text/html"}


async def test_verify_gate_blocks_select_tenant_for_unverified(billing_client) -> None:
    """GET /platform/select-tenant for an unverified Identity must redirect
    to /platform/verify-sent (the global 403→303 handler kicks in for
    HTML requests; non-HTML requests still see a 403 JSON)."""
    client, _ = billing_client
    await _signup_and_get_tenant(billing_client, email="o@unv1.cz", slug="unv1")

    resp = await client.get(
        "/platform/select-tenant",
        headers=HTML_HEADERS,
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/platform/verify-sent"


async def test_verify_gate_blocks_switch_for_unverified(billing_client) -> None:
    """POST /platform/switch/{slug} for an unverified Identity also bounces."""
    client, _ = billing_client
    await _signup_and_get_tenant(billing_client, email="o@unv2.cz", slug="unv2")

    resp = await client.post(
        "/platform/switch/unv2",
        data={"next": "/app"},
        headers=HTML_HEADERS,
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/platform/verify-sent"


async def test_verify_gate_blocks_complete_switch_for_unverified(billing_client) -> None:
    """GET /platform/complete-switch?token=... for an unverified Identity
    bounces too — even if the handoff token were valid the gate fires
    first."""
    client, _ = billing_client
    await _signup_and_get_tenant(billing_client, email="o@unv3.cz", slug="unv3")

    resp = await client.get(
        "/platform/complete-switch?token=junk",
        headers=HTML_HEADERS,
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/platform/verify-sent"


# ---------------------------------------------------------------------------
# F-BE-008: /platform/billing/details — validation + audit + gate
# ---------------------------------------------------------------------------


async def test_billing_details_get_renders_form(billing_client, owner_engine) -> None:
    """GET shows the form prefilled from tenant.settings (or company name)."""
    client, _ = billing_client
    await _signup_and_get_tenant(billing_client, email="o@bd1.cz", slug="bd1")
    await _mark_verified(owner_engine, "o@bd1.cz")

    resp = await client.get("/platform/billing/details", follow_redirects=False)
    assert resp.status_code == 200
    body = resp.text
    assert "billing_ico" in body
    assert "billing_name" in body
    # Default name comes from tenant.name when settings has no override.
    assert "TenantCo" in body


async def test_billing_details_post_happy_path_writes_settings(
    billing_client, owner_engine
) -> None:
    """A complete POST writes IČO/DIČ/name/address into tenant.settings,
    flips an audit-event row, and redirects to next= with a flash."""
    client, _ = billing_client
    await _signup_and_get_tenant(billing_client, email="o@bd2.cz", slug="bd2")
    await _mark_verified(owner_engine, "o@bd2.cz")

    resp = await client.post(
        "/platform/billing/details",
        data={
            "billing_name": "BD2 s.r.o.",
            "billing_ico": "12345678",
            "billing_dic": "cz12345678",  # case-insensitive — server uppercases
            "billing_address": "Lidická 2020/2, Děčín, 405 02, Česká republika",
            "next": "/platform/billing",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/platform/billing?notice=")

    # tenant.settings actually carries the cleaned values now.
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s:
        tenant = (await s.execute(select(Tenant).where(Tenant.slug == "bd2"))).scalar_one()
        assert tenant.settings["billing_ico"] == "12345678"
        assert tenant.settings["billing_dic"] == "CZ12345678"
        assert tenant.settings["billing_name"] == "BD2 s.r.o."
        assert "Lidická" in tenant.settings["billing_address"]

    # F-BE-001: an audit row should now exist for this tenant.
    async with sm() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT action FROM audit_events "
                    "WHERE tenant_id = (SELECT id FROM tenants WHERE slug='bd2') "
                    "AND action = 'tenant.settings_updated'"
                )
            )
        ).all()
        assert len(rows) >= 1


@pytest.mark.parametrize(
    "case_id,field,bad_value,error_fragment",
    [
        ("name-empty", "billing_name", "", "Fakturační název"),
        ("ico-short", "billing_ico", "1234", "IČO"),
        ("ico-nondigit", "billing_ico", "1234567a", "IČO"),
        ("addr-empty", "billing_address", "", "adresa"),
        ("dic-no-cz", "billing_dic", "12345678", "DIČ"),
    ],
)
async def test_billing_details_post_validation_branches(
    billing_client, owner_engine, case_id, field, bad_value, error_fragment
) -> None:
    """Each of the validation branches redirects back with ?error=...
    and does NOT write tenant.settings."""
    client, _ = billing_client
    short_id = case_id.replace("-", "")[:8]
    email = f"o@bd3{short_id}.cz"
    slug = f"bd3{short_id}"
    await _signup_and_get_tenant(billing_client, email=email, slug=slug)
    await _mark_verified(owner_engine, email)

    valid = {
        "billing_name": "BD3",
        "billing_ico": "12345678",
        "billing_dic": "",
        "billing_address": "Some street 1, Praha",
    }
    valid[field] = bad_value

    resp = await client.post(
        "/platform/billing/details",
        data=valid | {"next": "/platform/billing"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/platform/billing/details?error=" in resp.headers["location"]
    # Decoded query string should mention the relevant field.
    from urllib.parse import unquote

    assert error_fragment in unquote(resp.headers["location"])


async def test_checkout_gates_when_billing_details_missing(billing_client, owner_engine) -> None:
    """When STRIPE is enabled and tenant.settings is missing IČO etc.,
    /platform/billing/checkout/{plan} must redirect to the details form
    with a ?next= back-link, NOT silently fall through."""
    client, _sender = billing_client
    # Flip Stripe ON for this one test (the fixture leaves it off by
    # default for demo mode). The price IDs stay empty so we never reach
    # the Stripe API — the gate redirect short-circuits before that.
    from app.config import get_settings

    s = get_settings()
    s.stripe_secret_key = "sk_test_dummy_for_gate_check"
    try:
        await _signup_and_get_tenant(billing_client, email="o@bd4.cz", slug="bd4")
        await _mark_verified(owner_engine, "o@bd4.cz")

        resp = await client.post(
            "/platform/billing/checkout/starter",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert loc.startswith("/platform/billing/details?next=")
        assert "checkout%2Fstarter" in loc or "checkout/starter" in loc
    finally:
        s.stripe_secret_key = ""
