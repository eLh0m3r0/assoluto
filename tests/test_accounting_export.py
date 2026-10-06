"""POHODA / Money S3 XML export of received orders (MKT-3).

Two layers:

* the pure builders (``build_pohoda_datapack``, ``build_money_s3_xml``)
  — structure, encoding, escaping, schema length limits; no database;
* the routes ``/app/admin/exports/{pohoda,money-s3}.xml`` — filters,
  admin-only access, RLS tenant isolation.

Schema validation: the vendors' XSDs are not vendored here (their
redistribution terms are not stated). To run the strict validation
tests, have ``lxml`` importable (``uv run --with lxml pytest ...``) and
point ``POHODA_XSD_DIR`` at a directory with ``data.xsd`` + imports from
https://www.stormware.cz/schema/version_2/ and/or ``MONEY_S3_XSD_DIR``
at the unpacked ``Schemas`` folder of
https://money.cz/wp-content/uploads/2024/10/schemas.zip.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.customer import Customer, CustomerContact
from app.models.enums import CustomerContactRole, OrderStatus, UserRole
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.tenant import Tenant
from app.models.user import User
from app.security.passwords import hash_password
from app.services.accounting_export import (
    DEFAULT_STATUSES,
    ExportItem,
    ExportOrder,
    ExportPartner,
    NothingToExport,
    build_money_s3_xml,
    build_pohoda_datapack,
    parse_statuses,
)

NS = {
    "dat": "http://www.stormware.cz/schema/version_2/data.xsd",
    "ord": "http://www.stormware.cz/schema/version_2/order.xsd",
    "typ": "http://www.stormware.cz/schema/version_2/type.xsd",
}


def _order(**kw) -> ExportOrder:
    base = {
        "number": "2026-000001",
        "title": "Laser parts",
        "currency": "CZK",
        "order_date": date(2026, 10, 1),
        "partner": ExportPartner(company="ACME s.r.o.", ico="12345678", dic="CZ12345678"),
        "items": (
            ExportItem(
                text="Bracket",
                quantity=Decimal("12.500"),
                unit="ks",
                unit_price=Decimal("1234.50"),
                code="BR-1",
            ),
        ),
    }
    base.update(kw)
    return ExportOrder(**base)


def _parse(body: bytes) -> ET.Element:
    return ET.fromstring(body)


# ------------------------------------------------------------ pure builder


def test_envelope_and_received_order_structure() -> None:
    body = build_pohoda_datapack([_order()], pack_id="p1", ico="87654321")
    root = _parse(body)
    assert root.tag == f"{{{NS['dat']}}}dataPack"
    assert root.get("version") == "2.0"
    assert root.get("id") == "p1"
    assert root.get("ico") == "87654321"
    assert root.get("application") == "Assoluto"
    assert root.get("note")

    items = root.findall("dat:dataPackItem", NS)
    assert len(items) == 1
    assert items[0].get("version") == "2.0"
    order = items[0].find("ord:order", NS)
    assert order is not None and order.get("version") == "2.0"
    header = order.find("ord:orderHeader", NS)
    assert header.findtext("ord:orderType", namespaces=NS) == "receivedOrder"
    assert header.findtext("ord:numberOrder", namespaces=NS) == "2026-000001"
    assert header.findtext("ord:date", namespaces=NS) == "2026-10-01"
    addr = header.find("ord:partnerIdentity/typ:address", NS)
    assert addr.findtext("typ:company", namespaces=NS) == "ACME s.r.o."
    assert addr.findtext("typ:ico", namespaces=NS) == "12345678"
    assert addr.findtext("typ:dic", namespaces=NS) == "CZ12345678"
    # Absent address fields are omitted, not emitted empty.
    assert addr.find("typ:street", NS) is None
    assert addr.find("typ:city", NS) is None

    row = order.find("ord:orderDetail/ord:orderItem", NS)
    assert row.findtext("ord:text", namespaces=NS) == "Bracket"
    assert row.findtext("ord:quantity", namespaces=NS) == "12.5"
    assert row.findtext("ord:unit", namespaces=NS) == "ks"
    assert row.findtext("ord:payVAT", namespaces=NS) == "false"
    assert row.findtext("ord:rateVAT", namespaces=NS) == "none"
    assert row.findtext("ord:homeCurrency/typ:unitPrice", namespaces=NS) == "1234.5"
    assert row.findtext("ord:code", namespaces=NS) == "BR-1"
    # CZK → no foreign-currency summary.
    assert order.find("ord:orderSummary", NS) is None


def test_encoding_is_windows_1250_with_czech_diacritics() -> None:
    order = _order(
        title="Žluťoučký kůň",
        partner=ExportPartner(company="Šťastný & syn <s.r.o.>"),
    )
    body = build_pohoda_datapack([order], pack_id="p")
    assert body.startswith(b"<?xml version='1.0' encoding='windows-1250'?>")
    # Native cp1250 bytes, not UTF-8 multibyte sequences.
    assert "Žluťoučký kůň".encode("cp1250") in body
    assert "Žluťoučký".encode() not in body
    # Markup characters are escaped and round-trip intact.
    assert b"&amp; syn &lt;s.r.o.&gt;" in body
    root = _parse(body)
    company = root.find(".//typ:company", NS).text
    assert company == "Šťastný & syn <s.r.o.>"
    assert root.find(".//ord:text", NS).text == "Žluťoučký kůň"


def test_characters_outside_cp1250_become_char_refs_and_controls_are_dropped() -> None:
    body = build_pohoda_datapack([_order(note="Díky 😀\x0b konec")], pack_id="p")
    assert b"&#128512;" in body
    note = _parse(body).find(".//ord:note", NS).text
    assert note == "Díky 😀 konec"


def test_schema_length_limits_are_respected() -> None:
    order = _order(
        title="T" * 400,
        partner=ExportPartner(company="C", ico="1" * 20, city="X" * 80),
        items=(
            ExportItem(text="I" * 200, quantity=Decimal("1"), unit="kilogramy-x", code="S" * 70),
        ),
    )
    root = _parse(build_pohoda_datapack([order], pack_id="p" * 100))
    assert len(root.get("id")) <= 64
    assert len(root.find(".//ord:orderHeader/ord:text", NS).text) <= 240
    assert len(root.find(".//ord:orderItem/ord:text", NS).text) <= 90
    assert len(root.find(".//typ:city", NS).text) <= 45
    assert len(root.find(".//ord:unit", NS).text) <= 10
    # Identifiers are never truncated — a cut IČO / SKU would point at
    # something else. Too long → omitted.
    assert root.find(".//typ:ico", NS) is None
    assert root.find(".//ord:code", NS) is None


def test_vat_rate_and_foreign_currency() -> None:
    order = _order(currency="EUR")
    root = _parse(build_pohoda_datapack([order], pack_id="p", vat_rate="high"))
    row = root.find(".//ord:orderItem", NS)
    assert row.findtext("ord:rateVAT", namespaces=NS) == "high"
    assert row.findtext("ord:payVAT", namespaces=NS) == "false"
    assert row.find("ord:homeCurrency", NS) is None
    assert row.findtext("ord:foreignCurrency/typ:unitPrice", namespaces=NS) == "1234.5"
    assert (
        root.findtext(".//ord:orderSummary/ord:foreignCurrency/typ:currency/typ:ids", namespaces=NS)
        == "EUR"
    )


def test_order_without_items_or_prices() -> None:
    no_items = _order(items=())
    root = _parse(build_pohoda_datapack([no_items], pack_id="p"))
    assert root.find(".//ord:orderDetail", NS) is None

    no_price = _order(items=(ExportItem(text="Quote me", quantity=Decimal("3")),))
    row = _parse(build_pohoda_datapack([no_price], pack_id="p")).find(".//ord:orderItem", NS)
    assert row.find("ord:homeCurrency", NS) is None


def test_ico_omitted_when_blank_and_errors() -> None:
    root = _parse(build_pohoda_datapack([_order()], pack_id="p", ico=""))
    assert root.get("ico") is None
    with pytest.raises(NothingToExport):
        build_pohoda_datapack([], pack_id="p")
    with pytest.raises(ValueError):
        build_pohoda_datapack([_order()], pack_id="p", vat_rate="21")


def test_parse_statuses_defaults_skip_draft_and_cancelled() -> None:
    assert parse_statuses([]) == DEFAULT_STATUSES
    assert parse_statuses([""]) == DEFAULT_STATUSES
    assert OrderStatus.DRAFT not in DEFAULT_STATUSES
    assert OrderStatus.CANCELLED not in DEFAULT_STATUSES
    # P3-12: only confirmed and later by default; the rest is opt-in.
    assert DEFAULT_STATUSES == (
        OrderStatus.CONFIRMED,
        OrderStatus.IN_PRODUCTION,
        OrderStatus.READY,
        OrderStatus.DELIVERED,
        OrderStatus.CLOSED,
    )
    assert parse_statuses(["submitted", "quoted"]) == (OrderStatus.SUBMITTED, OrderStatus.QUOTED)
    assert parse_statuses(["draft", "bogus", "draft"]) == (OrderStatus.DRAFT,)


_XSD_DIR = os.environ.get("POHODA_XSD_DIR")


@pytest.mark.skipif(not _XSD_DIR, reason="POHODA_XSD_DIR not set (XSDs are not vendored)")
def test_validates_against_official_xsd() -> None:
    etree = pytest.importorskip("lxml.etree")
    schema = etree.XMLSchema(etree.parse(str(Path(_XSD_DIR) / "data.xsd")))
    orders = [
        _order(
            title="Výroba & <test>",
            note="Poznámka",
            date_to=date(2026, 10, 20),
            partner=ExportPartner(
                company="Žluťoučký kůň s.r.o.",
                ico="12345678",
                dic="CZ12345678",
                street="Hlavní 1",
                city="Praha",
                zip="110 00",
            ),
        ),
        _order(number="2026-000002", currency="EUR", items=()),
    ]
    for rate in ("none", "high", "low"):
        doc = etree.fromstring(build_pohoda_datapack(orders, pack_id="p", ico="1", vat_rate=rate))
        assert schema.validate(doc), schema.error_log


# ------------------------------------------------------- Money S3 builder


def test_money_s3_structure_and_order_of_header_elements() -> None:
    order = _order(
        title="Laser parts",
        note="Rush",
        date_to=date(2026, 10, 20),
        partner=ExportPartner(
            company="Šťastný & syn <s.r.o.>",
            ico="12345678",
            dic="CZ12345678",
            street="Hlavní 1",
            city="Děčín",
            zip="405 02",
        ),
    )
    body = build_money_s3_xml([order], ico="87654321", vat_rate="high")
    assert body.startswith(b"<?xml version='1.0' encoding='utf-8'?>")
    assert "Šťastný".encode() in body
    assert b"&amp; syn &lt;s.r.o.&gt;" in body

    root = _parse(body)
    assert root.tag == "MoneyData"
    assert root.get("ICAgendy") == "87654321"
    obj = root.find("SeznamObjPrij/ObjPrij")
    # ObjPrij is an xs:sequence — the order of children is part of the
    # contract with the schema.
    assert [c.tag for c in obj] == [
        "Popis",
        "Poznamka",
        "Vystaveno",
        "Vyridit_do",
        "DodOdb",
        "PrimDoklad",
        "Polozka",
    ]
    assert obj.findtext("PrimDoklad") == "2026-000001"
    assert obj.findtext("Vystaveno") == "2026-10-01"
    firm = obj.find("DodOdb")
    assert firm.findtext("ObchNazev") == "Šťastný & syn <s.r.o.>"
    assert firm.findtext("ICO") == "12345678"
    assert firm.findtext("DIC") == "CZ12345678"
    assert firm.findtext("FaktAdresa/Misto") == "Děčín"
    pol = obj.find("Polozka")
    assert pol.findtext("Popis") == "Bracket"
    assert pol.findtext("PocetMJ") == "12.5"
    assert pol.findtext("Cena") == "1234.5"
    assert pol.findtext("SazbaDPH") == "21"
    assert pol.findtext("TypCeny") == "0"
    assert pol.findtext("NesklPolozka/MJ") == "ks"
    assert pol.findtext("NesklPolozka/Katalog") == "BR-1"


def test_money_s3_vat_long_text_and_foreign_currency() -> None:
    long_text = "Plech " + "y" * 80
    order = _order(
        currency="EUR",
        items=(
            ExportItem(
                text=long_text, quantity=Decimal("2"), unit_price=Decimal("10.50"), note="n1"
            ),
        ),
    )
    root = _parse(build_money_s3_xml([order]))
    assert root.get("ICAgendy") is None
    obj = root.find("SeznamObjPrij/ObjPrij")
    assert obj.findtext("Valuty/Mena/Kod") == "EUR"
    assert obj.findtext("Valuty/Celkem") == "21"
    pol = obj.find("Polozka")
    assert pol.findtext("SazbaDPH") == "0"  # default: not a VAT payer
    assert pol.find("Cena") is None
    assert pol.findtext("Valuty") == "10.5"
    assert len(pol.findtext("Popis")) <= 50
    # Nothing the customer wrote is lost: the full text goes to the note.
    assert pol.findtext("Poznamka") == f"{long_text}\nn1"

    low = _parse(build_money_s3_xml([_order()], vat_rate="low"))
    assert low.findtext(".//Polozka/SazbaDPH") == "12"
    with pytest.raises(NothingToExport):
        build_money_s3_xml([])
    with pytest.raises(ValueError):
        build_money_s3_xml([_order()], vat_rate="21")


_MONEY_XSD_DIR = os.environ.get("MONEY_S3_XSD_DIR")


@pytest.mark.skipif(not _MONEY_XSD_DIR, reason="MONEY_S3_XSD_DIR not set (XSDs are not vendored)")
def test_money_s3_validates_against_official_xsd() -> None:
    etree = pytest.importorskip("lxml.etree")
    schema = etree.XMLSchema(etree.parse(str(Path(_MONEY_XSD_DIR) / "_Document.xsd")))
    orders = [
        _order(
            note="Pozn",
            date_to=date(2026, 10, 20),
            partner=ExportPartner(
                company="Žluťoučký kůň", ico="12345678", street="Hlavní 1", city="Praha"
            ),
        ),
        _order(number="2026-000002", currency="EUR"),
        _order(number="2026-000003", items=(), partner=ExportPartner(company="")),
    ]
    for rate in ("none", "high", "low"):
        doc = etree.fromstring(build_money_s3_xml(orders, ico="1", vat_rate=rate))
        assert schema.validate(doc), schema.error_log


# ------------------------------------------------------------- HTTP layer


async def _seed(owner_engine, tenant_id) -> dict:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s, s.begin():
        await s.execute(
            update(Tenant)
            .where(Tenant.id == tenant_id)
            .values(settings={"billing_ico": "99887766"})
        )
        admin = User(
            id=uuid4(),
            tenant_id=tenant_id,
            email="admin@4mex.cz",
            full_name="Admin",
            role=UserRole.TENANT_ADMIN,
            password_hash=hash_password("adminpass"),
        )
        staff = User(
            id=uuid4(),
            tenant_id=tenant_id,
            email="staff@4mex.cz",
            full_name="Operator",
            role=UserRole.TENANT_STAFF,
            password_hash=hash_password("staffpass"),
        )
        acme = Customer(
            id=uuid4(),
            tenant_id=tenant_id,
            name="Šťastný & syn s.r.o.",
            ico="11111111",
            dic="CZ11111111",
            billing_address={"street": "Hlavní 1", "city": "Děčín", "zip": "405 02"},
        )
        other = Customer(id=uuid4(), tenant_id=tenant_id, name="Other", ico="22222222")
        s.add_all([admin, staff, acme, other])
        await s.flush()
        contact = CustomerContact(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=acme.id,
            email="jan@acme.cz",
            full_name="Jan",
            role=CustomerContactRole.CUSTOMER_ADMIN,
            password_hash=hash_password("contactpass"),
            invited_at=datetime.now(UTC),
            accepted_at=datetime.now(UTC),
        )
        product = Product(id=uuid4(), tenant_id=tenant_id, sku="BR-1", name="Bracket", unit="ks")
        s.add_all([contact, product])
        await s.flush()

        specs = [
            ("2026-000001", acme, OrderStatus.CONFIRMED, datetime(2026, 9, 1, 10, tzinfo=UTC)),
            ("2026-000002", acme, OrderStatus.DRAFT, datetime(2026, 9, 2, 10, tzinfo=UTC)),
            ("2026-000003", other, OrderStatus.CANCELLED, datetime(2026, 9, 3, 10, tzinfo=UTC)),
            ("2026-000004", other, OrderStatus.DELIVERED, datetime(2026, 9, 20, 10, tzinfo=UTC)),
        ]
        ids = {}
        for number, cust, status, created in specs:
            o = Order(
                id=uuid4(),
                tenant_id=tenant_id,
                customer_id=cust.id,
                number=number,
                title=f"Zakázka {number}",
                status=status,
                created_at=created,
            )
            s.add(o)
            ids[number] = o.id
        await s.flush()
        s.add(
            OrderItem(
                tenant_id=tenant_id,
                order_id=ids["2026-000001"],
                product_id=product.id,
                position=0,
                description="BR-1 — Bracket",
                quantity=Decimal("4"),
                unit="ks",
                unit_price=Decimal("150.00"),
            )
        )
        return {"acme_id": acme.id, "other_id": other.id}


async def _seed_foreign_tenant(owner_engine) -> None:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s, s.begin():
        t = Tenant(
            id=uuid4(),
            slug="rival",
            name="Rival",
            billing_email="b@rival.cz",
            storage_prefix="tenants/rival/",
        )
        s.add(t)
        await s.flush()
        c = Customer(id=uuid4(), tenant_id=t.id, name="Rival Secret Client", ico="33333333")
        s.add(c)
        await s.flush()
        s.add(
            Order(
                id=uuid4(),
                tenant_id=t.id,
                customer_id=c.id,
                number="RIVAL-1",
                title="Secret",
                status=OrderStatus.CONFIRMED,
            )
        )


async def _login(client: AsyncClient, email: str, password: str) -> None:
    resp = await client.post(
        "/auth/login", data={"email": email, "password": password}, follow_redirects=False
    )
    assert resp.status_code == 303, resp.text


def _numbers(body: bytes) -> list[str]:
    return [el.text for el in _parse(body).iter(f"{{{NS['ord']}}}numberOrder")]


pg = pytest.mark.postgres


@pg
async def test_admin_downloads_pohoda_xml(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _seed_foreign_tenant(owner_engine)
    await _login(tenant_client, "admin@4mex.cz", "adminpass")

    resp = await tenant_client.get("/app/admin/exports/pohoda.xml")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    assert "windows-1250" in resp.headers["content-type"]
    disp = resp.headers["content-disposition"]
    assert disp.startswith('attachment; filename="pohoda-objednavky-')
    assert disp.endswith('.xml"')

    body = resp.content
    # Defaults: DRAFT and CANCELLED skipped; oldest first.
    assert _numbers(body) == ["2026-000001", "2026-000004"]
    # RLS: the other tenant's order never leaks.
    assert b"RIVAL" not in body
    assert "Rival Secret Client".encode("cp1250") not in body

    root = _parse(body)
    assert root.get("ico") == "99887766"  # tenant billing IČO
    first = root.find("dat:dataPackItem/ord:order", NS)
    addr = first.find("ord:orderHeader/ord:partnerIdentity/typ:address", NS)
    assert addr.findtext("typ:company", namespaces=NS) == "Šťastný & syn s.r.o."
    assert addr.findtext("typ:city", namespaces=NS) == "Děčín"
    assert addr.findtext("typ:zip", namespaces=NS) == "405 02"
    assert addr.findtext("typ:ico", namespaces=NS) == "11111111"
    row = first.find("ord:orderDetail/ord:orderItem", NS)
    assert row.findtext("ord:code", namespaces=NS) == "BR-1"
    assert row.findtext("ord:text", namespaces=NS) == "Bracket"
    assert row.findtext("ord:homeCurrency/typ:unitPrice", namespaces=NS) == "150"


@pg
async def test_unconfirmed_orders_are_opt_in(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """P3-12: a request waiting for a price or an open quote is not a
    received order — left out by default, exported when ticked."""
    ids = await _seed(owner_engine, demo_tenant.id)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s, s.begin():
        for number, status in (
            ("2026-000005", OrderStatus.SUBMITTED),
            ("2026-000006", OrderStatus.QUOTED),
            ("2026-000007", OrderStatus.READY),
        ):
            s.add(
                Order(
                    id=uuid4(),
                    tenant_id=demo_tenant.id,
                    customer_id=ids["acme_id"],
                    number=number,
                    title=f"Zakázka {number}",
                    status=status,
                    created_at=datetime(2026, 9, 25, 10, tzinfo=UTC),
                )
            )
    await _login(tenant_client, "admin@4mex.cz", "adminpass")

    default = await tenant_client.get("/app/admin/exports/pohoda.xml")
    assert _numbers(default.content) == ["2026-000001", "2026-000004", "2026-000007"]
    money = await tenant_client.get("/app/admin/exports/money-s3.xml")
    assert b"2026-000005" not in money.content and b"2026-000006" not in money.content

    picked = await tenant_client.get("/app/admin/exports/pohoda.xml?status=submitted&status=quoted")
    assert sorted(_numbers(picked.content)) == ["2026-000005", "2026-000006"]

    import re

    page = (await tenant_client.get("/app/admin/exports")).text
    checked = set(re.findall(r'name="status" value="(\w+)"\s+checked', page))
    assert checked == {"confirmed", "in_production", "ready", "delivered", "closed"}


@pg
async def test_filters_status_customer_and_dates(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    ids = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "admin@4mex.cz", "adminpass")

    resp = await tenant_client.get("/app/admin/exports/pohoda.xml?status=draft&status=confirmed")
    assert _numbers(resp.content) == ["2026-000001", "2026-000002"]

    resp = await tenant_client.get(f"/app/admin/exports/pohoda.xml?customer={ids['other_id']}")
    assert _numbers(resp.content) == ["2026-000004"]

    resp = await tenant_client.get("/app/admin/exports/pohoda.xml?from=2026-09-10&to=2026-09-30")
    assert _numbers(resp.content) == ["2026-000004"]

    resp = await tenant_client.get("/app/admin/exports/pohoda.xml?vat_rate=high&ico=")
    root = _parse(resp.content)
    assert root.get("ico") is None
    assert {el.text for el in root.iter(f"{{{NS['ord']}}}rateVAT")} == {"high"}


@pg
async def test_nothing_to_export_redirects_with_flash(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "admin@4mex.cz", "adminpass")

    resp = await tenant_client.get(
        "/app/admin/exports/pohoda.xml?from=2030-01-01", follow_redirects=False
    )
    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert loc.startswith("/app/admin/exports?")
    assert "from=2030-01-01" in loc and "error=" in loc

    resp = await tenant_client.get(
        "/app/admin/exports/pohoda.xml?vat_rate=21", follow_redirects=False
    )
    assert resp.status_code == 303


@pg
async def test_exports_page_and_order_list_button_for_admin(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "admin@4mex.cz", "adminpass")

    page = await tenant_client.get("/app/admin/exports?error=Nope")
    assert page.status_code == 200
    assert 'action="/app/admin/exports/pohoda.xml"' in page.text
    assert 'value="99887766"' in page.text
    assert "Nope" in page.text

    listing = await tenant_client.get("/app/orders?status=confirmed")
    assert "/app/admin/exports/pohoda.xml?status=confirmed" in listing.text


@pg
async def test_non_admin_staff_gets_403(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")

    assert (await tenant_client.get("/app/admin/exports/pohoda.xml")).status_code == 403
    assert (await tenant_client.get("/app/admin/exports")).status_code == 403
    listing = await tenant_client.get("/app/orders")
    assert "/app/admin/exports/pohoda.xml" not in listing.text


@pg
async def test_contact_is_refused(tenant_client: AsyncClient, owner_engine, demo_tenant) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "jan@acme.cz", "contactpass")

    resp = await tenant_client.get("/app/admin/exports/pohoda.xml", follow_redirects=False)
    assert resp.status_code == 403
    assert b"dataPack" not in resp.content
    resp = await tenant_client.get("/app/admin/exports", follow_redirects=False)
    assert resp.status_code == 403


@pg
async def test_admin_downloads_money_s3_xml(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _seed_foreign_tenant(owner_engine)
    await _login(tenant_client, "admin@4mex.cz", "adminpass")

    resp = await tenant_client.get("/app/admin/exports/money-s3.xml?vat_rate=high")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    assert 'filename="money-s3-objednavky-' in resp.headers["content-disposition"]
    root = _parse(resp.content)
    assert root.get("ICAgendy") == "99887766"
    numbers = [el.text for el in root.iter("PrimDoklad")]
    assert numbers == ["2026-000001", "2026-000004"]
    assert b"RIVAL" not in resp.content
    pol = root.find("SeznamObjPrij/ObjPrij/Polozka")
    assert pol.findtext("NesklPolozka/Katalog") == "BR-1"
    assert pol.findtext("SazbaDPH") == "21"


@pg
async def test_money_s3_refused_for_staff_and_contact(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    assert (await tenant_client.get("/app/admin/exports/money-s3.xml")).status_code == 403

    await tenant_client.post("/auth/logout", follow_redirects=False)
    tenant_client.cookies.clear()
    await _login(tenant_client, "jan@acme.cz", "contactpass")
    resp = await tenant_client.get("/app/admin/exports/money-s3.xml", follow_redirects=False)
    assert resp.status_code == 403
    assert b"MoneyData" not in resp.content
