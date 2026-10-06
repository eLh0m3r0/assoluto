"""Locale-aware money in PDFs (E4, round 2).

PDF amounts follow the document locale via Babel/CLDR instead of the
old ``12345.50 CZK`` everywhere. Rounding stays half-up (LOGIC-17) and
the gaps are U+00A0 so an amount never wraps between digit groups.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.enums import OrderStatus
from app.models.order import Order, OrderItem
from app.models.tenant import Tenant
from app.services.pdf_service import format_money, render_order_pdf

NBSP = "\u00a0"


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("cs", f"12{NBSP}345,50{NBSP}Kč"),
        ("de", f"12.345,50{NBSP}CZK"),
        ("en", f"CZK{NBSP}12,345.50"),
    ],
)
def test_format_money_per_locale(locale: str, expected: str) -> None:
    assert format_money(Decimal("12345.5"), "CZK", locale=locale) == expected


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("cs", f"1{NBSP}000,00{NBSP}€"),
        ("de", f"1.000,00{NBSP}€"),
        ("en", "€1,000.00"),  # a symbol is not spaced from the digits
    ],
)
def test_format_money_symbol_currency(locale: str, expected: str) -> None:
    assert format_money(Decimal("1000"), "EUR", locale=locale) == expected


def test_format_money_rounds_half_up_in_every_locale() -> None:
    # Babel itself rounds half-even (0.125 -> 0.12); we must not.
    assert format_money(Decimal("0.125"), "CZK", locale="cs") == f"0,13{NBSP}Kč"
    assert format_money(Decimal("0.125"), "CZK", locale="de") == f"0,13{NBSP}CZK"
    assert format_money(Decimal("0.125"), "CZK", locale="en") == f"CZK{NBSP}0.13"
    assert format_money(Decimal("-2.345"), "CZK", locale="en") == f"-CZK{NBSP}2.35"


def test_format_money_edge_values() -> None:
    assert format_money(None, "CZK", locale="en") == ""
    assert format_money(Decimal("NaN"), "CZK", locale="cs") == ""
    assert format_money(Decimal("Infinity"), "CZK", locale="de") == ""
    # Always two decimals, also for whole amounts and without currency.
    assert format_money(Decimal("5"), None, locale="en") == "5.00"
    assert format_money(Decimal("1234"), None, locale="de") == "1.234,00"
    # Lower-case code; an unknown locale falls back to Czech.
    assert format_money(Decimal("1"), "czk", locale="xx") == f"1,00{NBSP}Kč"
    assert format_money(Decimal("1"), "CZK", locale="en-GB") == f"CZK{NBSP}1.00"


def _order_pdf(locale: str) -> bytes:
    tenant_id = uuid4()
    tenant = Tenant(id=tenant_id, slug="x", name="T", billing_email="b@b.cz", storage_prefix="t/")
    order = Order(
        id=uuid4(),
        tenant_id=tenant_id,
        customer_id=None,
        number="2026-000001",
        title="t",
        status=OrderStatus.QUOTED,
        quoted_total=Decimal("12345678.50"),
        currency="CZK",
    )
    order.created_at = datetime.now(UTC)
    order.submitted_at = None
    order.promised_delivery_at = None
    item = OrderItem(
        id=uuid4(),
        tenant_id=tenant_id,
        order_id=order.id,
        position=0,
        description="Díl",
        quantity=Decimal("1"),
        unit="ks",
        unit_price=Decimal("12345678.50"),
        line_total=Decimal("12345678.50"),
    )
    return render_order_pdf(order, [item], None, tenant, locale=locale)


def _pdf_text(pdf: bytes) -> str:
    out = subprocess.run(
        ["pdftotext", "-layout", "-", "-"], input=pdf, capture_output=True, check=True
    ).stdout.decode()
    # pdftotext may hand the no-break space back as either character.
    return out.replace(NBSP, " ")


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="poppler-utils not installed")
@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("cs", "12 345 678,50 Kč"),
        ("de", "12.345.678,50 CZK"),
        ("en", "CZK 12,345,678.50"),
    ],
)
def test_order_pdf_prints_locale_money_on_one_line(locale: str, expected: str) -> None:
    """Unit price, line total and subtotal each render whole (glyphs
    present, no wrap inside the number) in the document's locale."""
    text = _pdf_text(_order_pdf(locale))
    assert text.count(expected) == 3, text
    assert "12345678.50" not in text


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="poppler-utils not installed")
@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("cs", "1 490,00 Kč"),
        ("de", "1.490,00 CZK"),
        ("en", "CZK 1,490.00"),
    ],
)
def test_invoice_pdf_prints_locale_money(locale: str, expected: str) -> None:
    from app.services.invoice_pdf_service import render_invoice_pdf

    now = datetime.now(UTC)
    invoice = SimpleNamespace(
        id="x",
        stripe_invoice_id="in_X",
        number="2026-000001",
        amount_cents=149_000,
        currency="CZK",
        status="paid",
        paid_at=now,
        created_at=now,
    )
    tenant = SimpleNamespace(name="Dílna s.r.o.", settings={})
    settings = SimpleNamespace(
        platform_operator_name="Op",
        platform_operator_ico="12345678",
        platform_operator_dic="",
        platform_operator_address="Ulice 1, Praha",
        platform_operator_email="op@example.com",
    )
    pdf = render_invoice_pdf(
        invoice=invoice,  # type: ignore[arg-type]
        tenant=tenant,  # type: ignore[arg-type]
        settings=settings,  # type: ignore[arg-type]
        locale=locale,
    )
    text = _pdf_text(pdf)
    # Non-VAT supplier: the amount appears in the line and the total.
    assert text.count(expected) >= 2, text
    # The line-item row used to fall back to Helvetica (no Czech glyphs).
    assert "\u25a0" not in text, text
    if locale == "cs":
        assert "Předplatné" in text
