"""Locale-aware ``qty`` / ``percent`` / ``timeago`` filters (demo review
P2-2 / P2-4). Pure unit tests — no database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.templating import _new_environment

NBSP = "\u00a0"


def _render(locale: str | None, source: str, **ctx) -> str:
    return _new_environment(locale).from_string(source).render(**ctx)


@pytest.mark.parametrize(
    ("locale", "value", "expected"),
    [
        ("cs", Decimal("24.500"), "24,5"),
        ("cs", Decimal("1200.000"), f"1{NBSP}200"),
        ("cs", Decimal("75.000"), "75"),
        ("de", Decimal("24.500"), "24,5"),
        ("de", Decimal("1200"), "1.200"),
        ("en", Decimal("1200.250"), "1,200.25"),
        ("en", Decimal("0.001"), "0.001"),
        ("cs", 3, "3"),
        ("cs", None, ""),
        ("cs", "n/a", "n/a"),
        ("cs", Decimal("NaN"), "NaN"),
    ],
)
def test_qty_is_localised(locale, value, expected) -> None:
    assert _render(locale, "{{ v|qty }}", v=value) == expected


@pytest.mark.parametrize("locale", ["cs", "de", "en"])
def test_qty_input_stays_machine_readable(locale) -> None:
    """``<input type=number value=…>`` needs a dot and no grouping."""
    out = _render(locale, "{{ v|qty_input }}", v=Decimal("1200.500"))
    assert out == "1200.5"


@pytest.mark.parametrize(
    ("locale", "value", "expected"),
    [
        ("cs", 1.0, f"100{NBSP}%"),
        ("cs", 0.9534, f"95,3{NBSP}%"),
        ("cs", 0, f"0{NBSP}%"),
        ("de", 1.0, f"100{NBSP}%"),
        ("de", 2 / 3, f"66,7{NBSP}%"),
        ("en", 1.0, "100%"),
        ("en", 0.5, "50%"),
        ("en", None, "\u2013"),
    ],
)
def test_percent_is_localised(locale, value, expected) -> None:
    assert _render(locale, "{{ v|percent }}", v=value) == expected


def test_percent_decimals_argument() -> None:
    assert _render("en", "{{ v|percent(0) }}", v=0.956) == "96%"
    assert _render("en", "{{ v|percent(2) }}", v=0.95678) == "95.68%"


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (1, "před 1 dnem"),
        (2, "před 2 dny"),
        (5, "před 5 dny"),
        (13, "před 13 dny"),
    ],
)
def test_timeago_czech_day_plural(days, expected) -> None:
    moment = datetime.now(UTC) - timedelta(days=days, minutes=5)
    assert _render("cs", "{{ v|timeago }}", v=moment) == expected


def test_timeago_other_locales_unchanged() -> None:
    moment = datetime.now(UTC) - timedelta(days=1, minutes=5)
    assert _render("en", "{{ v|timeago }}", v=moment) == "1d ago"
    assert _render("de", "{{ v|timeago }}", v=moment) == "vor 1 Tg."
    assert _render(None, "{{ v|timeago }}", v=moment) == "1d ago"
