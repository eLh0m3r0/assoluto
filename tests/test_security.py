"""Unit tests for password hashing and signed tokens."""

from __future__ import annotations

import time

import pytest

from app.security.passwords import hash_password, needs_rehash, verify_password
from app.security.tokens import (
    ExpiredToken,
    InvalidToken,
    TokenPurpose,
    create_token,
    verify_token,
)

# --------------------------------------------------------------- passwords


def test_hash_and_verify_roundtrip() -> None:
    h = hash_password("correct horse battery staple")
    assert h.startswith("$argon2")
    assert verify_password("correct horse battery staple", h) is True


def test_verify_rejects_wrong_password() -> None:
    h = hash_password("abc")
    assert verify_password("abcd", h) is False


def test_verify_rejects_none_hash() -> None:
    assert verify_password("anything", None) is False


def test_verify_rejects_malformed_hash() -> None:
    assert verify_password("x", "not-a-hash") is False


def test_empty_password_rejected_at_hashing() -> None:
    with pytest.raises(ValueError):
        hash_password("")


def test_each_hash_is_unique_due_to_salt() -> None:
    a = hash_password("same")
    b = hash_password("same")
    assert a != b
    assert verify_password("same", a)
    assert verify_password("same", b)


def test_needs_rehash_false_for_fresh_hash() -> None:
    h = hash_password("fresh")
    assert needs_rehash(h) is False


# ----------------------------------------------------------------- tokens

SECRET = "test-secret-change-me"


def test_token_roundtrip() -> None:
    token = create_token(SECRET, TokenPurpose.INVITE, {"contact_id": "abc"})
    data = verify_token(SECRET, TokenPurpose.INVITE, token, max_age_seconds=60)
    assert data == {"contact_id": "abc"}


def test_token_with_wrong_purpose_is_invalid() -> None:
    token = create_token(SECRET, TokenPurpose.INVITE, {"x": 1})
    with pytest.raises(InvalidToken):
        verify_token(SECRET, TokenPurpose.MAGIC_LINK, token, max_age_seconds=60)


def test_token_with_wrong_secret_is_invalid() -> None:
    token = create_token(SECRET, TokenPurpose.INVITE, {"x": 1})
    with pytest.raises(InvalidToken):
        verify_token("other-secret", TokenPurpose.INVITE, token, max_age_seconds=60)


def test_token_expiry() -> None:
    token = create_token(SECRET, TokenPurpose.INVITE, {"x": 1})
    # Sleep slightly longer than max_age to guarantee the integer second
    # boundary has been crossed.
    time.sleep(2.1)
    with pytest.raises(ExpiredToken):
        verify_token(SECRET, TokenPurpose.INVITE, token, max_age_seconds=1)


def test_token_payload_must_be_dict_on_verify() -> None:
    # Encode a list payload directly and try to decode — should raise.
    import json

    from itsdangerous import URLSafeTimedSerializer

    bad = URLSafeTimedSerializer(SECRET, salt=TokenPurpose.INVITE).dumps(["not", "a", "dict"])
    with pytest.raises(InvalidToken):
        verify_token(SECRET, TokenPurpose.INVITE, bad, max_age_seconds=60)
    # silence unused-import warning for json when running coverage
    assert json is json
