"""Rate-limit tests.

The limiter is globally disabled when ``APP_ENV=test`` so the rest of
the suite doesn't have to worry about counter state. These tests
explicitly re-enable it and then restore the flag so they don't leak.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport

from app.main import create_app
from tests.conftest import CsrfAwareClient


async def test_contact_form_is_rate_limited(settings) -> None:
    """/contact allows 5 POSTs / 15 min / IP, then 429."""
    # Build an app with the limiter explicitly ON (overriding is_test).
    app = create_app(settings)
    from app.security.rate_limit import limiter

    try:
        limiter.enabled = True
        limiter.reset()

        transport = ASGITransport(app=app)
        async with CsrfAwareClient(transport=transport, base_url="http://testserver") as ac:
            # 5 legitimate-shaped requests should all succeed (200 or 400).
            # We send invalid data on purpose so they return 400 — the
            # limiter counts attempts regardless of outcome.
            for _ in range(5):
                r = await ac.post(
                    "/contact",
                    data={"name": "X", "email": "x@y.cz", "message": "hi"},
                )
                assert r.status_code in (200, 400)

            # The 6th hits the limiter.
            r = await ac.post(
                "/contact",
                data={"name": "X", "email": "x@y.cz", "message": "hi"},
            )
            assert r.status_code == 429
            assert r.headers.get("Retry-After") is not None
    finally:
        limiter.enabled = False
        limiter.reset()


async def test_limiter_is_disabled_in_test_env(settings) -> None:
    """Sanity check: by default the test suite runs with the limiter off.

    Without this escape hatch every integration test would have to reset
    counters or space out POSTs. APP_ENV=test → limiter.enabled = False.
    """
    app = create_app(settings)
    from app.security.rate_limit import limiter

    # install() was called by create_app with enabled=not is_test.
    assert limiter.enabled is False

    transport = ASGITransport(app=app)
    async with CsrfAwareClient(transport=transport, base_url="http://testserver") as ac:
        # 10 contact posts should all go through (422 or 400) — never 429.
        for _ in range(10):
            r = await ac.post(
                "/contact",
                data={"name": "X", "email": "x@y.cz", "message": "hi"},
            )
            assert r.status_code != 429, f"limiter leaked into test env: {r.status_code}"


@pytest.mark.postgres
async def test_signup_is_rate_limited(settings, wipe_db, owner_engine) -> None:
    """POST /platform/signup: 10 / 15 min / IP."""
    from sqlalchemy import text

    settings.feature_platform = True
    async with owner_engine.begin() as conn:
        await conn.execute(text("DELETE FROM platform_tenant_memberships"))
        await conn.execute(text("DELETE FROM platform_identities"))
        await conn.execute(text("DELETE FROM platform_subscriptions"))
        await conn.execute(text("DELETE FROM platform_invoices"))

    from app.platform.deps import reset_platform_engine

    reset_platform_engine()

    app = create_app(settings)
    from app.security.rate_limit import limiter

    try:
        limiter.enabled = True
        limiter.reset()

        transport = ASGITransport(app=app)
        async with CsrfAwareClient(transport=transport, base_url="http://testserver") as ac:
            # 10 failing signups (invalid form) should all come back 400.
            for i in range(10):
                r = await ac.post(
                    "/platform/signup",
                    data={
                        "company_name": "x",  # too short -> 400
                        "owner_email": f"rl{i}@rl.cz",
                        "password": "correct-horse-battery-staple",
                        "terms_accepted": "1",
                    },
                )
                assert r.status_code in (400, 303)

            # 11th is throttled.
            r = await ac.post(
                "/platform/signup",
                data={
                    "company_name": "OK Ltd",
                    "slug": "okltd",
                    "owner_email": "rl@rl.cz",
                    "password": "correct-horse-battery-staple",
                    "terms_accepted": "1",
                },
            )
            assert r.status_code == 429
    finally:
        limiter.enabled = False
        limiter.reset()
        reset_platform_engine()
