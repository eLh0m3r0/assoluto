"""UX-04 / F-39 / BE-22: platform signup + auth flows are translatable.

Two guards:

* a static AST scan that fails when a Czech string literal (outside a
  docstring) reappears in the platform signup / login / reset / admin
  routers or the signup validators — the regression the audit found
  twice;
* behavioural checks that an English-locale visitor gets English error
  messages from those flows instead of a Czech sentence inside an
  English page.
"""

from __future__ import annotations

import ast
import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport
from sqlalchemy import text

from app.email.sender import CaptureSender
from app.main import create_app
from tests.conftest import CsrfAwareClient

REPO = Path(__file__).resolve().parent.parent
SCANNED = [
    REPO / "app" / "platform" / "routers" / "signup.py",
    REPO / "app" / "platform" / "routers" / "platform_auth.py",
    REPO / "app" / "platform" / "routers" / "platform_admin.py",
    REPO / "app" / "platform" / "validation.py",
    REPO / "app" / "platform" / "deps.py",
]
_CZECH = re.compile(r"[ěščřžýáíéůúňťďĚŠČŘŽÝÁÍÉŮÚŇŤĎ]")


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and node.body
        ):
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


@pytest.mark.parametrize("path", SCANNED, ids=lambda p: p.name)
def test_no_hardcoded_czech_literals(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = _docstring_nodes(tree)
    offenders = [
        f"{path.name}:{node.lineno}: {node.value!r}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and _CZECH.search(node.value)
    ]
    assert offenders == [], "wrap these in _t(request, ...) with an English msgid:\n" + "\n".join(
        offenders
    )


def test_validation_error_localizes_with_params() -> None:
    from app.platform.validation import SignupValidationError, validate_slug

    with pytest.raises(SignupValidationError) as excinfo:
        validate_slug("ab")
    # Untranslated (identity) rendering fills the placeholder.
    assert excinfo.value.localized(lambda m: m) == "Subdomain must be at least 3 characters long."
    # The translator sees the msgid with the placeholder intact.
    seen: list[str] = []
    excinfo.value.localized(lambda m: seen.append(m) or m)
    assert seen == ["Subdomain must be at least %(n)s characters long."]


# ------------------------------------------------------------ behavioural


@pytest.fixture
async def en_client(settings, wipe_db, owner_engine) -> AsyncIterator[CsrfAwareClient]:
    settings.feature_platform = True
    async with owner_engine.begin() as conn:
        await conn.execute(text("DELETE FROM platform_tenant_memberships"))
        await conn.execute(text("DELETE FROM platform_identities"))
    from app.platform.deps import reset_platform_engine

    reset_platform_engine()
    app = create_app(settings)
    app.state.email_sender = CaptureSender()
    transport = ASGITransport(app=app)
    async with CsrfAwareClient(transport=transport, base_url="http://testserver") as ac:
        ac.cookies.set("sme_locale", "en")
        yield ac
    reset_platform_engine()


@pytest.mark.postgres
async def test_signup_validation_error_is_english_for_en_visitor(en_client) -> None:
    resp = await en_client.post(
        "/platform/signup",
        data={
            "company_name": "Acme",
            "slug": "admin",  # reserved
            "owner_email": "owner@acme.test",
            "owner_full_name": "Owner",
            "password": "correct-horse-battery-staple-42",
            "terms_accepted": "on",
        },
    )
    assert resp.status_code == 400
    assert "This subdomain is reserved." in resp.text
    assert "rezervovaná" not in resp.text


@pytest.mark.postgres
async def test_verify_email_bad_token_is_english_for_en_visitor(en_client) -> None:
    resp = await en_client.get("/platform/verify-email?token=garbage")
    assert resp.status_code == 400
    assert "The verification link is invalid." in resp.text


@pytest.mark.postgres
async def test_password_reset_notice_is_english_for_en_visitor(en_client) -> None:
    resp = await en_client.post("/platform/password-reset", data={"email": "nobody@example.test"})
    assert resp.status_code == 200
    assert "If the address exists, we have sent a password reset link." in resp.text
    assert "Pokud adresa existuje" not in resp.text


@pytest.mark.postgres
async def test_password_reset_confirm_mismatch_is_english(en_client) -> None:
    resp = await en_client.post(
        "/platform/password-reset/confirm",
        data={"token": "x", "password": "abcdefgh1", "password_confirm": "different1"},
    )
    assert resp.status_code == 400
    assert "Passwords do not match." in resp.text
