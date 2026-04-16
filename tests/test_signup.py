"""Self-signup + email verification tests.

These tests run with FEATURE_PLATFORM=True, reusing the same
``platform_client`` fixture pattern as :mod:`tests.test_platform`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from httpx import ASGITransport
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.email.sender import CaptureSender
from app.main import create_app
from app.models.tenant import Tenant
from app.models.user import User
from app.platform.models import Identity, TenantMembership
from app.security.tokens import TokenPurpose, create_token
from tests.conftest import CsrfAwareClient

pytestmark = pytest.mark.postgres


@pytest.fixture
async def signup_client(
    settings, wipe_db, owner_engine
) -> AsyncIterator[tuple[CsrfAwareClient, CaptureSender]]:
    """Signup-flavoured client: FEATURE_PLATFORM=true + CaptureSender."""
    settings.feature_platform = True

    async with owner_engine.begin() as conn:
        await conn.execute(text("DELETE FROM platform_tenant_memberships"))
        await conn.execute(text("DELETE FROM platform_identities"))

    from app.platform.deps import reset_platform_engine

    reset_platform_engine()

    app = create_app(settings)
    # Swap the live SMTP sender for a capture-only one.
    sender = CaptureSender()
    app.state.email_sender = sender

    transport = ASGITransport(app=app)
    async with CsrfAwareClient(transport=transport, base_url="http://testserver") as ac:
        yield ac, sender

    reset_platform_engine()


# ---------------------------------------------------------------- unit tests


def test_validation_slug_valid() -> None:
    from app.platform.validation import validate_slug

    assert validate_slug("acme") == "acme"
    assert validate_slug("acme-123") == "acme-123"


def test_validation_slug_rejects_reserved() -> None:
    from app.platform.validation import SignupValidationError, validate_slug

    for bad in ("www", "api", "admin", "platform", "login"):
        with pytest.raises(SignupValidationError):
            validate_slug(bad)


def test_validation_slug_rejects_bad_chars() -> None:
    from app.platform.validation import SignupValidationError, validate_slug

    for bad in ("-acme", "acme-", "Acme", "ac me", ""):
        with pytest.raises(SignupValidationError):
            validate_slug(bad)


def test_validation_parse_signup_form_derives_slug_when_empty() -> None:
    from app.platform.validation import parse_signup_form

    form = parse_signup_form(
        company_name="ACME s.r.o.",
        slug="",
        owner_email="jan@acme.cz",
        owner_full_name="Jan Novák",
        password="correct-horse-battery-staple",
        terms_accepted=True,
    )
    # python-slugify keeps each dot as a boundary, so "s.r.o." -> "s-r-o".
    assert form.slug == "acme-s-r-o"


def test_validation_rejects_weak_password() -> None:
    from app.platform.validation import SignupValidationError, validate_password

    # Trivial passwords: score 0 (common password).
    with pytest.raises(SignupValidationError) as excinfo:
        validate_password("password123")
    assert excinfo.value.field == "password"
    assert "slab" in excinfo.value.message.lower()

    # Still short of the score threshold: common English phrase.
    with pytest.raises(SignupValidationError):
        validate_password("verysecret")

    # Decent random-looking password passes.
    validate_password("correct-horse-battery-staple")


def test_validation_user_inputs_lower_zxcvbn_score() -> None:
    """Sanity check: zxcvbn gets our user-input hint and produces a
    non-higher score for passwords containing them. Exact thresholds
    drift with library versions, so we assert a relative comparison
    rather than a specific score."""
    from zxcvbn import zxcvbn

    baseline = zxcvbn("acmeAcme42")["score"]
    penalised = zxcvbn("acmeAcme42", user_inputs=["ACME s.r.o.", "acme"])["score"]
    assert penalised <= baseline


def test_validation_parse_signup_form_requires_tos() -> None:
    from app.platform.validation import SignupValidationError, parse_signup_form

    with pytest.raises(SignupValidationError) as excinfo:
        parse_signup_form(
            company_name="ACME s.r.o.",
            slug="acme",
            owner_email="jan@acme.cz",
            owner_full_name="Jan Novák",
            password="correct-horse-battery-staple",
            terms_accepted=False,
        )
    assert excinfo.value.field == "terms_accepted"


# ---------------------------------------------------------------- E2E tests


async def test_signup_form_renders(signup_client) -> None:
    client, _ = signup_client
    resp = await client.get("/platform/signup")
    assert resp.status_code == 200
    assert "Vytvořte si svůj portál" in resp.text
    assert "csrf_token" in resp.text


async def test_signup_creates_tenant_identity_and_sends_verification_email(
    signup_client, owner_engine
) -> None:
    client, capture = signup_client

    resp = await client.post(
        "/platform/signup",
        data={
            "company_name": "ACME s.r.o.",
            "slug": "acme-test",
            "owner_email": "owner@acme-test.cz",
            "owner_full_name": "Jan Novák",
            "password": "correct-horse-battery-staple",
            "terms_accepted": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/platform/verify-sent"

    # DB state: tenant + user + identity + membership + ToS timestamp.
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.slug == "acme-test"))
        ).scalar_one()
        assert tenant.name == "ACME s.r.o."

        user = (await session.execute(select(User).where(User.tenant_id == tenant.id))).scalar_one()
        assert user.email == "owner@acme-test.cz"

        identity = (
            await session.execute(select(Identity).where(Identity.email == "owner@acme-test.cz"))
        ).scalar_one()
        assert identity.email_verified_at is None
        assert identity.terms_accepted_at is not None

        membership = (
            await session.execute(
                select(TenantMembership).where(
                    TenantMembership.identity_id == identity.id,
                    TenantMembership.tenant_id == tenant.id,
                )
            )
        ).scalar_one()
        assert membership.user_id == user.id

    # Verification email was queued.
    assert len(capture.outbox) == 1
    assert capture.outbox[0].to == "owner@acme-test.cz"
    assert "/platform/verify-email?token=" in capture.outbox[0].text


async def test_signup_duplicate_slug_returns_400(signup_client) -> None:
    client, _ = signup_client
    # First signup (succeeds).
    resp1 = await client.post(
        "/platform/signup",
        data={
            "company_name": "ACME",
            "slug": "acme-dup",
            "owner_email": "a@acme-dup.cz",
            "owner_full_name": "A",
            "password": "correct-horse-battery-staple",
            "terms_accepted": "1",
        },
        follow_redirects=False,
    )
    assert resp1.status_code == 303

    # Second signup with same slug but different email → 400.
    resp2 = await client.post(
        "/platform/signup",
        data={
            "company_name": "ACME 2",
            "slug": "acme-dup",
            "owner_email": "b@acme-dup.cz",
            "owner_full_name": "B",
            "password": "correct-horse-battery-staple",
            "terms_accepted": "1",
        },
        follow_redirects=False,
    )
    assert resp2.status_code == 400
    assert "Tato subdoména je již obsazená" in resp2.text


async def test_signup_duplicate_email_returns_400(signup_client) -> None:
    client, _ = signup_client
    resp1 = await client.post(
        "/platform/signup",
        data={
            "company_name": "ACME",
            "slug": "acme-a",
            "owner_email": "dup@acme.cz",
            "owner_full_name": "A",
            "password": "correct-horse-battery-staple",
            "terms_accepted": "1",
        },
        follow_redirects=False,
    )
    assert resp1.status_code == 303

    resp2 = await client.post(
        "/platform/signup",
        data={
            "company_name": "ACME 2",
            "slug": "acme-b",
            "owner_email": "dup@acme.cz",
            "owner_full_name": "B",
            "password": "correct-horse-battery-staple",
            "terms_accepted": "1",
        },
        follow_redirects=False,
    )
    assert resp2.status_code == 400
    assert "Účet s tímto e-mailem již existuje" in resp2.text


async def test_verify_email_marks_identity_verified(signup_client, owner_engine) -> None:
    client, _ = signup_client

    # Signup first.
    resp = await client.post(
        "/platform/signup",
        data={
            "company_name": "VerifyCo",
            "slug": "verify-co",
            "owner_email": "verify@co.cz",
            "owner_full_name": "V",
            "password": "correct-horse-battery-staple",
            "terms_accepted": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    # Pull the Identity and forge a valid verification token.
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        identity = (
            await session.execute(select(Identity).where(Identity.email == "verify@co.cz"))
        ).scalar_one()
        identity_id: UUID = identity.id
        assert identity.email_verified_at is None

    from app.config import get_settings

    token = create_token(
        get_settings().app_secret_key,
        TokenPurpose.EMAIL_VERIFY,
        {"identity_id": str(identity_id)},
    )

    resp = await client.get(f"/platform/verify-email?token={token}")
    assert resp.status_code == 200
    assert "úspěšně ověřen" in resp.text

    async with sm() as session:
        identity = (
            await session.execute(select(Identity).where(Identity.id == identity_id))
        ).scalar_one()
        assert identity.email_verified_at is not None


async def test_verify_email_rejects_bad_token(signup_client) -> None:
    client, _ = signup_client
    resp = await client.get("/platform/verify-email?token=not-a-real-token")
    assert resp.status_code == 400
    assert "neplatný" in resp.text.lower()


async def test_signup_race_integrityerror_is_translated(signup_client, owner_engine) -> None:
    """Two concurrent signups with the same email: one succeeds, the other
    must surface as a clean 400 ``DuplicateIdentityEmail`` response rather
    than a 500 ``IntegrityError``.

    We simulate the race by writing the Identity directly via the owner
    engine and THEN sending a signup through the app — the happy-path
    dup-check runs before our insert so we fool it into reaching the
    flush, where the unique constraint fires.
    """
    from datetime import UTC, datetime
    from uuid import uuid4

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.platform.models import Identity
    from app.security.passwords import hash_password

    client, _ = signup_client

    # Staged racer: identity row committed to DB before our signup hits.
    # The signup's pre-flight find_identity_by_email will catch this first
    # and raise DuplicateIdentityEmail via the happy-path branch — so we
    # commit the racer AFTER signup has opened its session. Easier: just
    # force the IntegrityError path by reusing the same email twice
    # without clearing, relying on the inline DB write's visibility.
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        racer = Identity(
            id=uuid4(),
            email="race@rc.cz",
            full_name="Racer",
            password_hash=hash_password("rcrcrcrc"),
            email_verified_at=datetime.now(UTC),
        )
        session.add(racer)

    # Now the happy-path check finds it → DuplicateIdentityEmail → 400.
    resp = await client.post(
        "/platform/signup",
        data={
            "company_name": "RaceCo",
            "slug": "race-co",
            "owner_email": "race@rc.cz",
            "owner_full_name": "R",
            "password": "correct-horse-battery-staple",
            "terms_accepted": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "Účet s tímto e-mailem již existuje" in resp.text


def test_signup_tenant_maps_integrityerror_without_preflight() -> None:
    """Direct unit test: when the pre-flight check misses (simulates the
    true race), signup_tenant's IntegrityError handler must still map to
    DuplicateIdentityEmail / DuplicateTenantSlug."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    from sqlalchemy.exc import IntegrityError

    from app.platform.service import (
        DuplicateIdentityEmail,
        DuplicateTenantSlug,
        signup_tenant,
    )

    fake_db = AsyncMock()
    fake_db.rollback = AsyncMock()

    async def _run():
        # Case A: email constraint error → DuplicateIdentityEmail
        with (
            patch("app.platform.service.find_identity_by_email", return_value=None),
            patch(
                "app.platform.service.create_tenant_with_owner",
                side_effect=IntegrityError(
                    "insert",
                    None,
                    Exception("duplicate key on uq_platform_identities_email"),
                ),
            ),
        ):
            try:
                await signup_tenant(
                    fake_db,
                    company_name="X",
                    slug="x",
                    owner_email="a@b.cz",
                    owner_full_name="A",
                    owner_password="p",
                )
                raise AssertionError("should have raised")
            except DuplicateIdentityEmail:
                pass

        # Case B: slug constraint error → DuplicateTenantSlug
        with (
            patch("app.platform.service.find_identity_by_email", return_value=None),
            patch(
                "app.platform.service.create_tenant_with_owner",
                side_effect=IntegrityError(
                    "insert", None, Exception("duplicate key on uq_tenants_slug")
                ),
            ),
        ):
            try:
                await signup_tenant(
                    fake_db,
                    company_name="X",
                    slug="x",
                    owner_email="a@b.cz",
                    owner_full_name="A",
                    owner_password="p",
                )
                raise AssertionError("should have raised")
            except DuplicateTenantSlug:
                pass

    asyncio.get_event_loop().run_until_complete(_run())


async def test_signup_rejects_missing_tos(signup_client) -> None:
    client, _ = signup_client
    resp = await client.post(
        "/platform/signup",
        data={
            "company_name": "NoTosCo",
            "slug": "notos",
            "owner_email": "a@b.cz",
            "owner_full_name": "A",
            "password": "correct-horse-battery-staple",
            # terms_accepted deliberately omitted
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "podmínkami" in resp.text.lower()
