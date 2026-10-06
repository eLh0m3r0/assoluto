"""Orders CSV follows the user's spreadsheet conventions (P3-11)."""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import update

from app.models.order import Order
from app.routers.orders import _fmt_date, _fmt_datetime, _fmt_decimal, csv_dialect
from tests.test_orders_export import _login, _seed


def test_dialect_per_locale() -> None:
    for loc in ("cs", "de", "cs-CZ"):
        d = csv_dialect(loc)
        assert (d.delimiter, d.decimal_comma, d.day_first) == (";", True, True)
    for loc in ("en", None, "xx"):
        d = csv_dialect(loc)
        assert (d.delimiter, d.decimal_comma, d.day_first) == (",", False, False)


def test_value_formats() -> None:
    cs, en = csv_dialect("cs"), csv_dialect("en")
    assert _fmt_decimal(Decimal("1234.50"), cs) == "1234,50"
    assert _fmt_decimal(Decimal("1234.50"), en) == "1234.50"
    assert _fmt_date(date(2026, 10, 18), cs) == "18.10.2026"
    assert _fmt_date(date(2026, 10, 18), en) == "2026-10-18"
    instant = datetime(2026, 3, 2, 23, 30, tzinfo=UTC)
    assert _fmt_datetime(instant, None, cs) == "03.03.2026 00:30"
    assert _fmt_datetime(instant, None, en) == "2026-03-03T00:30:00+01:00"


async def _prepare(tenant_client, owner_engine, demo_tenant) -> None:
    await _seed(owner_engine, demo_tenant.id)
    async with owner_engine.begin() as conn:
        await conn.execute(
            update(Order)
            .where(Order.title == "ACME quoted")
            .values(quoted_total=Decimal("1234.50"), promised_delivery_at=date(2026, 10, 18))
        )
    await _login(tenant_client, "owner@4mex.cz", "ownerpass")


def _rows(body: str, delimiter: str) -> list[list[str]]:
    assert body.startswith("﻿")  # UTF-8 BOM for Excel
    return list(csv.reader(io.StringIO(body.lstrip("﻿")), delimiter=delimiter))


@pytest.mark.postgres
async def test_czech_csv_is_excel_ready(tenant_client, owner_engine, demo_tenant) -> None:
    await _prepare(tenant_client, owner_engine, demo_tenant)
    resp = await tenant_client.get("/app/orders.csv", headers={"Accept-Language": "cs"})
    rows = _rows(resp.text, ";")
    quoted = next(r for r in rows if r[0] == "2026-000002")
    assert quoted[1] != "quoted"  # a label, not the enum value
    assert quoted[5] == "18.10.2026"
    assert quoted[6] == "1234,50"
    assert len(quoted[3]) == len("dd.mm.yyyy HH:MM") and quoted[3][2] == "."


@pytest.mark.postgres
async def test_english_csv_uses_comma_and_dot(tenant_client, owner_engine, demo_tenant) -> None:
    await _prepare(tenant_client, owner_engine, demo_tenant)
    resp = await tenant_client.get("/app/orders.csv", headers={"Accept-Language": "en"})
    rows = _rows(resp.text, ",")
    assert rows[0][0] == "Order number"
    quoted = next(r for r in rows if r[0] == "2026-000002")
    assert quoted[1] == "Quoted"
    assert quoted[5] == "2026-10-18"
    assert quoted[6] == "1234.50"
    drafts = [r for r in rows[1:] if r[1] == "Draft"]
    assert len(drafts) == 4
