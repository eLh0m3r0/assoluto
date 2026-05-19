"""Unit tests for the contact-form spam filters."""

from __future__ import annotations

import pytest

from app.security.contact_filter import (
    DISPOSABLE_EMAIL_DOMAINS,
    is_disposable_email,
    looks_like_bot_local_part,
)


@pytest.mark.parametrize(
    "email",
    [
        "user@mailinator.com",
        "USER@MAILINATOR.COM",  # case-insensitive
        "  joanna@yopmail.com  ",  # trimmed
        "x@example.com",
        "anything@guerrillamail.net",
        "spam@10minutemail.com",
        "test@temp-mail.org",
    ],
)
def test_disposable_detected(email: str) -> None:
    assert is_disposable_email(email) is True


@pytest.mark.parametrize(
    "email",
    [
        "vaclav.mudra@firma.cz",
        "info@assoluto.eu",
        "j.smith@gmail.com",
        "team@somecompany.de",
        "real-person@protonmail.com",
        "",
        "not-an-email",
        "@no-local.com",
        "no-domain@",
    ],
)
def test_legit_or_malformed_not_flagged_as_disposable(email: str) -> None:
    assert is_disposable_email(email) is False


@pytest.mark.parametrize(
    "email",
    [
        "ftgrgxbafx@gmail.com",  # 10 chars — still under threshold, no
        "info@somewhere.com",  # short
        "vaclav.mudra@firma.cz",  # has dot
        "j-smith@firma.cz",  # has dash
        "user+tag@firma.cz",  # has plus
        "first_last@firma.cz",  # has underscore
        "",
        "not-an-email",
    ],
)
def test_human_local_part_not_flagged(email: str) -> None:
    assert looks_like_bot_local_part(email) is False


@pytest.mark.parametrize(
    "email",
    [
        "ftgrgxbafxqp@gmail.com",  # 12-char random local
        "abcdefghijklmnop@gmail.com",  # 16-char
        "QWERTYUIOPAS@gmail.com",  # uppercase variant (we lower-case)
    ],
)
def test_bot_local_part_flagged(email: str) -> None:
    assert looks_like_bot_local_part(email) is True


def test_disposable_list_is_lowercased_and_unique() -> None:
    """Catch typos / case drift at test time so the lookup stays O(1)
    against a normalised key."""
    for domain in DISPOSABLE_EMAIL_DOMAINS:
        assert domain == domain.lower(), domain
        assert " " not in domain, domain
        assert domain.count("@") == 0, domain
