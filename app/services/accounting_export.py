"""Export orders to Czech accounting software (POHODA, Money S3).

The accountant's complaint that blocks a sale is "I would have to retype
every order". POHODA imports *received orders* (agenda *Přijaté
objednávky*) from an XML ``dataPack``; this module turns a selection of
Assoluto orders into exactly that file, so the import is one dialog in
POHODA instead of manual data entry.

Format reference (all fetched 2026-10-04):

* Overview of importable documents, incl. the "Objednávky" samples:
  https://www.stormware.cz/pohoda/xml/dokladyimport/
* Order schema (``ord:order``, version 2.0):
  https://www.stormware.cz/schema/version_2/order.xsd
* Envelope schema (``dat:dataPack`` / ``dat:dataPackItem``, version 2.0):
  https://www.stormware.cz/schema/version_2/data.xsd
* Shared types (``typ:address``, ``typ:vatRateType``, string lengths):
  https://www.stormware.cz/schema/version_2/type.xsd
* Official sample of a received order with items:
  https://www.stormware.cz/xml/samples/version_2/import/Objednavky/order_01_v2.0.xml
* Encoding and the ``ico`` envelope attribute ("XML data jsou uložena v
  kódování Windows-1250"; ``ico`` "vybírá účetní jednotku ... do které
  se budou data načítat"):
  https://www.stormware.cz/pohoda/xml/obecny-obchod/pro-vyvojare/
* User-facing import dialog (Soubor → Datová komunikace → XML
  import/export…):
  https://www.stormware.cz/prirucka-pohoda-online/datova_komunikace/xml_import-export/

Mapping decisions (see also ``docs/POHODA_EXPORT.md``):

* One ``dataPackItem`` per order; ``ord:orderType`` = ``receivedOrder``.
* ``ord:numberOrder`` = the Assoluto order number (the customer-facing
  reference). POHODA assigns its own document number from its series.
* ``ord:date`` = date the customer submitted the order (falls back to
  creation date); ``ord:dateTo`` ("Vyřídit do") = promised delivery
  date, else the requested one.
* Partner identity is filled only from fields the ``Customer`` row
  actually has (name, IČO, DIČ, street/city/zip from the free-form
  ``billing_address`` JSON). Absent fields are omitted, never invented.
  ``country`` is omitted because POHODA expects a code from its own
  country list.
* Items are *text items* (no stock link): ``ord:text`` (max 90 chars),
  quantity, unit, unit price, catalogue SKU as ``ord:code``.
* Prices are exported as entered, ``payVAT=false`` (without VAT). The
  VAT rate defaults to ``none`` — Assoluto has no tenant VAT model, and a
  non-VAT payer must not have VAT added on import. A VAT payer picks
  ``high``/``low`` on the export page and POHODA computes the VAT.
* CZK prices go to ``homeCurrency``; any other currency goes to
  ``foreignCurrency`` and the order summary names the currency so
  POHODA uses its exchange-rate list (no rate is exported).
* The file is encoded in Windows-1250 as the documentation states.
  Characters outside that code page are written as numeric character
  references by the serializer, so nothing is lost.

Money S3 (Seyfor) — :func:`build_money_s3_xml`, same selection, native
``MoneyData`` format which needs no XMLDE module. Sources (fetched
2026-10-04):

* Developer page with current XSDs and samples:
  https://money.cz/navod/s3xmlde/
* XSDs (``_Document.xsd`` root, ``__Objedn.xsd`` → ``objednavkaType``):
  https://money.cz/wp-content/uploads/2024/10/schemas.zip
* Samples, incl. ``OBJP_sklad_neskl.xml`` (received order, UTF-8):
  https://money.cz/wp-content/uploads/2024/10/vzorove_xml.zip
* Manual "XML elektronická výměna dat" (import via Nástroje → Výměna
  dat XML → Import; *Doklad došlý* as a matching key for received
  documents): https://money.cz/wp-content/uploads/2023/07/xml_prenosy.pdf

Money S3 mapping: ``ObjPrij`` per order; Assoluto number →
``PrimDoklad`` (the 10-char ``Doklad`` is left for Money's own series);
title → ``Popis`` (max 50); items as non-stock items with ``SazbaDPH``
as a percentage (0 / 21 / 12) and ``TypCeny=0`` (price without VAT);
SKU → ``NesklPolozka/Katalog``. UTF-8 like the official samples.

The XML builders are pure functions over plain dataclasses so they can
be tested without a database; :func:`load_orders_for_export` does the
DB side under the caller's RLS-scoped session.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer
from app.models.enums import OrderStatus
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.services.order_service import ActorRef, build_orders_query

NS_DAT = "http://www.stormware.cz/schema/version_2/data.xsd"
NS_ORD = "http://www.stormware.cz/schema/version_2/order.xsd"
NS_TYP = "http://www.stormware.cz/schema/version_2/type.xsd"

POHODA_ENCODING = "windows-1250"
POHODA_MEDIA_TYPE = "application/xml; charset=windows-1250"
HOME_CURRENCY = "CZK"

#: ``typ:vatRateEnum`` values that make sense for a Czech received order.
#: (``third`` / ``history*`` exist in the schema but are SK-only or
#: historical.)
VAT_RATES: tuple[str, ...] = ("none", "high", "low")
DEFAULT_VAT_RATE = "none"

#: Statuses exported when the caller does not choose any. A draft is not
#: an order yet and a cancelled order must not reach the books.
DEFAULT_EXCLUDED_STATUSES: frozenset[OrderStatus] = frozenset(
    {OrderStatus.DRAFT, OrderStatus.CANCELLED}
)
DEFAULT_STATUSES: tuple[OrderStatus, ...] = tuple(
    s for s in OrderStatus if s not in DEFAULT_EXCLUDED_STATUSES
)

#: Hard cap on one export. Keeps memory bounded; the accountant exports a
#: period at a time anyway.
MAX_EXPORT_ORDERS = 5000

# Max lengths from type.xsd / order.xsd.
_LEN_HEADER_TEXT = 240  # typ:string240
_LEN_ITEM_TEXT = 90  # typ:string90
_LEN_ITEM_NOTE = 90  # typ:string90
_LEN_CODE = 64  # typ:stockIdsType
_LEN_UNIT = 10  # typ:unitType
_LEN_NUMBER_ORDER = 32  # typ:documentNumberType
_LEN_COMPANY = 255  # typ:stringCompany
_LEN_CITY = 45  # typ:string45
_LEN_STREET = 64  # typ:string64
_LEN_ZIP = 15  # typ:string15
_LEN_ICO = 15  # typ:icoType
_LEN_DIC = 18  # typ:dicType
_LEN_ID = 64  # typ:string64 (dataPack/@id, dataPackItem/@id)
_LEN_APPLICATION = 100  # typ:string100

for _prefix, _uri in (("dat", NS_DAT), ("ord", NS_ORD), ("typ", NS_TYP)):
    ET.register_namespace(_prefix, _uri)


class ExportError(Exception):
    """Base class for export failures the router turns into a flash."""


class NothingToExport(ExportError):
    """The filters matched no orders (an empty dataPack is invalid XML)."""


class TooManyOrders(ExportError):
    """The filters matched more than :data:`MAX_EXPORT_ORDERS` orders."""


# --------------------------------------------------------------- data shapes


@dataclass(frozen=True)
class ExportPartner:
    company: str
    ico: str | None = None
    dic: str | None = None
    street: str | None = None
    city: str | None = None
    zip: str | None = None


@dataclass(frozen=True)
class ExportItem:
    text: str
    quantity: Decimal
    unit: str | None = None
    unit_price: Decimal | None = None
    code: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class ExportOrder:
    number: str
    title: str
    currency: str
    order_date: date
    partner: ExportPartner
    items: tuple[ExportItem, ...] = field(default_factory=tuple)
    date_to: date | None = None
    note: str | None = None


@dataclass(frozen=True)
class ExportFilters:
    statuses: tuple[OrderStatus, ...] = DEFAULT_STATUSES
    customer_id: UUID | None = None
    date_from: date | None = None
    date_to: date | None = None
    q: str | None = None
    assigned_to: UUID | str | None = None


# ---------------------------------------------------------------- helpers


def _clip(value: str | None, limit: int) -> str | None:
    """Collapse whitespace and cut to the schema's max length."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _fits(value: str | None, limit: int) -> str | None:
    """Return a stripped identifier only if it fits — never truncate an ID.

    A truncated IČO would silently point at a different company, which
    is worse than leaving the field for the accountant to fill in.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or len(text) > limit:
        return None
    return text


def _num(value: Decimal) -> str:
    """Plain decimal string (no exponent) — valid for xsd:float/double."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _local_date(value: datetime) -> date:
    """Calendar date of a timestamp as a Czech accountant sees it."""
    try:
        from zoneinfo import ZoneInfo

        return value.astimezone(ZoneInfo("Europe/Prague")).date()
    except Exception:  # pragma: no cover - tzdata missing on a slim image
        return value.astimezone(UTC).date() if value.tzinfo else value.date()


# Characters XML 1.0 forbids even as character references (C0 controls
# other than tab/LF/CR, lone surrogates, U+FFFE/U+FFFF). ElementTree
# would write them verbatim and POHODA would reject the whole file.
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def _xml_safe(text: str) -> str:
    return _XML_ILLEGAL.sub("", text)


def _sub(parent: ET.Element, ns: str, tag: str, text: str | None = None) -> ET.Element:
    el = ET.SubElement(parent, f"{{{ns}}}{tag}")
    if text is not None:
        el.text = _xml_safe(text)
    return el


def _address_from_json(blob: Any) -> tuple[str | None, str | None, str | None]:
    """(street, city, zip) from the free-form ``Customer.billing_address``.

    Same conventional keys the order PDF renders; anything else is
    ignored rather than guessed at.
    """
    if not isinstance(blob, dict):
        return None, None, None
    street = blob.get("street") or blob.get("line1")
    city = blob.get("city")
    zip_code = blob.get("zip") or blob.get("postal_code")
    return (
        str(street) if street else None,
        str(city) if city else None,
        str(zip_code) if zip_code else None,
    )


def _strip_sku_prefix(description: str, sku: str | None) -> str:
    """Items picked from the catalogue are stored as ``"<sku> — <name>"``.

    The SKU already travels in ``ord:code``; keep the 90 characters of
    ``ord:text`` for the name.
    """
    if sku:
        prefix = f"{sku} — "
        if description.startswith(prefix):
            return description[len(prefix) :]
    return description


# ------------------------------------------------------------- XML builder


def build_pohoda_datapack(
    orders: Sequence[ExportOrder],
    *,
    pack_id: str,
    ico: str | None = None,
    vat_rate: str = DEFAULT_VAT_RATE,
    application: str = "Assoluto",
    note: str = "Assoluto export",
) -> bytes:
    """Serialize ``orders`` as a POHODA ``dat:dataPack`` (Windows-1250).

    ``ico`` is the IČO of the *tenant's own* accounting unit; POHODA uses
    it to pick the company the data goes into. Omitted when unknown.
    """
    if not orders:
        raise NothingToExport("dataPack requires at least one dataPackItem")
    if vat_rate not in VAT_RATES:
        raise ValueError(f"unsupported VAT rate {vat_rate!r}")

    root = ET.Element(f"{{{NS_DAT}}}dataPack")
    root.set("version", "2.0")
    root.set("id", pack_id[:_LEN_ID])
    ico_clean = _fits(ico, _LEN_ICO)
    if ico_clean:
        root.set("ico", ico_clean)
    root.set("application", application[:_LEN_APPLICATION])
    root.set("note", _xml_safe(note))

    for order in orders:
        item_el = _sub(root, NS_DAT, "dataPackItem")
        item_el.set("version", "2.0")
        item_el.set("id", order.number[:_LEN_ID])

        ord_el = _sub(item_el, NS_ORD, "order")
        ord_el.set("version", "2.0")

        header = _sub(ord_el, NS_ORD, "orderHeader")
        _sub(header, NS_ORD, "orderType", "receivedOrder")
        _sub(header, NS_ORD, "numberOrder", order.number[:_LEN_NUMBER_ORDER])
        _sub(header, NS_ORD, "date", order.order_date.isoformat())
        if order.date_to is not None:
            _sub(header, NS_ORD, "dateTo", order.date_to.isoformat())
        title = _clip(order.title, _LEN_HEADER_TEXT)
        if title:
            _sub(header, NS_ORD, "text", title)

        partner = _sub(header, NS_ORD, "partnerIdentity")
        address = _sub(partner, NS_TYP, "address")
        p = order.partner
        for tag, value in (
            ("company", _clip(p.company, _LEN_COMPANY)),
            ("city", _clip(p.city, _LEN_CITY)),
            ("street", _clip(p.street, _LEN_STREET)),
            ("zip", _clip(p.zip, _LEN_ZIP)),
            ("ico", _fits(p.ico, _LEN_ICO)),
            ("dic", _fits(p.dic, _LEN_DIC)),
        ):
            if value:
                _sub(address, NS_TYP, tag, value)

        if order.note and order.note.strip():
            _sub(header, NS_ORD, "note", order.note.strip())
        _sub(header, NS_ORD, "intNote", f"Assoluto {order.number}")

        foreign = order.currency.upper() != HOME_CURRENCY
        price_block = "foreignCurrency" if foreign else "homeCurrency"

        if order.items:
            detail = _sub(ord_el, NS_ORD, "orderDetail")
            for it in order.items:
                row = _sub(detail, NS_ORD, "orderItem")
                _sub(row, NS_ORD, "text", _clip(it.text, _LEN_ITEM_TEXT) or "-")
                _sub(row, NS_ORD, "quantity", _num(it.quantity))
                unit = _clip(it.unit, _LEN_UNIT)
                if unit:
                    _sub(row, NS_ORD, "unit", unit)
                _sub(row, NS_ORD, "payVAT", "false")
                _sub(row, NS_ORD, "rateVAT", vat_rate)
                if it.unit_price is not None:
                    money = _sub(row, NS_ORD, price_block)
                    _sub(money, NS_TYP, "unitPrice", _num(it.unit_price))
                item_note = _clip(it.note, _LEN_ITEM_NOTE)
                if item_note:
                    _sub(row, NS_ORD, "note", item_note)
                code = _fits(it.code, _LEN_CODE)
                if code:
                    _sub(row, NS_ORD, "code", code)

        if foreign:
            summary = _sub(ord_el, NS_ORD, "orderSummary")
            fc = _sub(summary, NS_ORD, "foreignCurrency")
            cur = _sub(fc, NS_TYP, "currency")
            _sub(cur, NS_TYP, "ids", order.currency.upper())

    return ET.tostring(root, encoding=POHODA_ENCODING, xml_declaration=True)


# ------------------------------------------------------- Money S3 builder

MONEY_S3_ENCODING = "utf-8"
MONEY_S3_MEDIA_TYPE = "application/xml; charset=utf-8"

#: Czech VAT percentages behind the shared ``none``/``high``/``low``
#: choice (rates in force since 2024-01-01). Money S3 items carry the
#: percentage itself (``SazbaDPH``), not a symbolic level like POHODA.
MONEY_S3_VAT_PERCENT: dict[str, str] = {"none": "0", "high": "21", "low": "12"}

# Max lengths from __Objedn.xsd / __Comtypes.xsd (Money S3 schemas).
_M_LEN_POPIS = 50  # popisType
_M_LEN_PRIM_DOKLAD = 20  # ObjPrij/PrimDoklad ("doklad došlý")
_M_LEN_ICO = 10
_M_LEN_DIC = 20
_M_LEN_STREET = 50
_M_LEN_CITY = 40
_M_LEN_ZIP = 10
_M_LEN_UNIT = 10  # NesklPolozka/MJ
_M_LEN_KATALOG = 60  # NesklPolozka/Katalog
_M_LEN_CURRENCY = 4  # menaType/Kod


def _el(parent: ET.Element, tag: str, text: str | None = None) -> ET.Element:
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = _xml_safe(text)
    return el


def _money_address(parent: ET.Element, tag: str, p: ExportPartner) -> None:
    street = _clip(p.street, _M_LEN_STREET)
    city = _clip(p.city, _M_LEN_CITY)
    zip_code = _clip(p.zip, _M_LEN_ZIP)
    if not (street or city or zip_code):
        return
    addr = _el(parent, tag)
    for child, value in (("Ulice", street), ("Misto", city), ("PSC", zip_code)):
        if value:
            _el(addr, child, value)


def build_money_s3_xml(
    orders: Sequence[ExportOrder],
    *,
    ico: str | None = None,
    vat_rate: str = DEFAULT_VAT_RATE,
    description: str = "Assoluto export",
) -> bytes:
    """Serialize ``orders`` as Money S3 ``MoneyData/SeznamObjPrij`` (UTF-8).

    ``ObjPrij`` is an ``xs:sequence`` in ``__Objedn.xsd`` — element order
    below follows the schema and must not be shuffled.
    """
    if not orders:
        raise NothingToExport("nothing to export")
    if vat_rate not in MONEY_S3_VAT_PERCENT:
        raise ValueError(f"unsupported VAT rate {vat_rate!r}")
    vat_percent = MONEY_S3_VAT_PERCENT[vat_rate]

    root = ET.Element("MoneyData")
    ico_clean = _fits(ico, _LEN_ICO)
    if ico_clean:
        root.set("ICAgendy", ico_clean)
    root.set("description", _xml_safe(description))
    seznam = _el(root, "SeznamObjPrij")

    for order in orders:
        foreign = order.currency.upper() != HOME_CURRENCY
        obj = _el(seznam, "ObjPrij")
        title = _clip(order.title, _M_LEN_POPIS)
        if title:
            _el(obj, "Popis", title)
        if order.note and order.note.strip():
            _el(obj, "Poznamka", order.note.strip())
        _el(obj, "Vystaveno", order.order_date.isoformat())
        if order.date_to is not None:
            _el(obj, "Vyridit_do", order.date_to.isoformat())

        p = order.partner
        firm = _el(obj, "DodOdb")
        company = _clip(p.company, 255)
        if company:
            _el(firm, "ObchNazev", company)
        _money_address(firm, "ObchAdresa", p)
        if company:
            _el(firm, "FaktNazev", company)
        ico_p = _fits(p.ico, _M_LEN_ICO)
        if ico_p:
            _el(firm, "ICO", ico_p)
        dic_p = _fits(p.dic, _M_LEN_DIC)
        if dic_p:
            _el(firm, "DIC", dic_p)
        _money_address(firm, "FaktAdresa", p)
        if company:
            _el(firm, "Nazev", company)
        _money_address(firm, "Adresa", p)
        if len(firm) == 0:
            obj.remove(firm)

        _el(obj, "PrimDoklad", order.number[:_M_LEN_PRIM_DOKLAD])

        if foreign:
            # The schema makes SouhrnDPH + Celkem mandatory inside Valuty
            # although both are "IMPORT: NE" (ignored on import).
            total = sum(
                (it.quantity * it.unit_price for it in order.items if it.unit_price is not None),
                Decimal("0"),
            ).quantize(Decimal("0.01"))
            valuty = _el(obj, "Valuty")
            mena = _el(valuty, "Mena")
            _el(mena, "Kod", order.currency.upper()[:_M_LEN_CURRENCY])
            _el(valuty, "SouhrnDPH")
            _el(valuty, "Celkem", _num(total))

        for idx, it in enumerate(order.items, start=1):
            pol = _el(obj, "Polozka")
            text = " ".join(it.text.split()) or "-"
            popis = _clip(text, _M_LEN_POPIS) or "-"
            _el(pol, "Popis", popis)
            # Popis is capped at 50 chars; keep the full wording in the
            # (unbounded) note so nothing the customer wrote is lost.
            notes = [n for n in (text if popis != text else None, it.note) if n and n.strip()]
            if notes:
                _el(pol, "Poznamka", "\n".join(n.strip() for n in notes))
            _el(pol, "PocetMJ", _num(it.quantity))
            if it.unit_price is not None and not foreign:
                _el(pol, "Cena", _num(it.unit_price))
            _el(pol, "SazbaDPH", vat_percent)
            _el(pol, "TypCeny", "0")  # 0 = bez DPH
            _el(pol, "Poradi", str(idx))
            if it.unit_price is not None and foreign:
                _el(pol, "Valuty", _num(it.unit_price))
            unit = _clip(it.unit, _M_LEN_UNIT)
            code = _fits(it.code, _M_LEN_KATALOG)
            if unit or code:
                neskl = _el(pol, "NesklPolozka")
                if unit:
                    _el(neskl, "MJ", unit)
                if code:
                    _el(neskl, "Katalog", code)

    return ET.tostring(root, encoding=MONEY_S3_ENCODING, xml_declaration=True)


# ---------------------------------------------------------------- DB side


async def load_orders_for_export(
    db: AsyncSession,
    *,
    actor: ActorRef,
    filters: ExportFilters,
    limit: int = MAX_EXPORT_ORDERS,
) -> list[ExportOrder]:
    """Load the matching orders as builder input, oldest first.

    Runs on the request's RLS-scoped session, so another tenant's orders
    cannot be selected whatever the filters say. Uses the same base
    query as the order list / CSV export so filters mean the same thing.
    """
    stmt = build_orders_query(
        actor=actor,
        customer_id=filters.customer_id,
        date_from=filters.date_from,
        date_to=filters.date_to,
        q=filters.q,
        assigned_to=filters.assigned_to,
    )
    stmt = (
        stmt.where(Order.status.in_(list(filters.statuses)))
        .order_by(None)
        .order_by(Order.created_at.asc(), Order.number.asc())
        .limit(limit + 1)
    )
    orders = list((await db.execute(stmt)).scalars().all())
    if len(orders) > limit:
        raise TooManyOrders(f"more than {limit} orders match")
    if not orders:
        return []

    order_ids = [o.id for o in orders]
    customer_ids = {o.customer_id for o in orders}

    customers: dict[UUID, Customer] = {
        c.id: c
        for c in (await db.execute(select(Customer).where(Customer.id.in_(customer_ids))))
        .scalars()
        .all()
    }

    items_by_order: dict[UUID, list[OrderItem]] = {oid: [] for oid in order_ids}
    item_rows = (
        (
            await db.execute(
                select(OrderItem)
                .where(OrderItem.order_id.in_(order_ids))
                .order_by(OrderItem.order_id, OrderItem.position, OrderItem.created_at)
            )
        )
        .scalars()
        .all()
    )
    for it in item_rows:
        items_by_order[it.order_id].append(it)

    product_ids = {it.product_id for it in item_rows if it.product_id is not None}
    skus: dict[UUID, str] = {}
    if product_ids:
        rows = await db.execute(select(Product.id, Product.sku).where(Product.id.in_(product_ids)))
        skus = dict(rows.tuples().all())

    result: list[ExportOrder] = []
    for o in orders:
        cust = customers.get(o.customer_id)
        street, city, zip_code = _address_from_json(cust.billing_address if cust else None)
        partner = ExportPartner(
            company=cust.name if cust else "",
            ico=cust.ico if cust else None,
            dic=cust.dic if cust else None,
            street=street,
            city=city,
            zip=zip_code,
        )
        items = []
        for it in items_by_order.get(o.id, []):
            sku = skus.get(it.product_id) if it.product_id else None
            items.append(
                ExportItem(
                    text=_strip_sku_prefix(it.description, sku),
                    quantity=it.quantity,
                    unit=it.unit,
                    unit_price=it.unit_price,
                    code=sku,
                    note=it.notes,
                )
            )
        stamp = o.submitted_at or o.created_at
        result.append(
            ExportOrder(
                number=o.number,
                title=o.title,
                currency=o.currency or HOME_CURRENCY,
                order_date=_local_date(stamp),
                date_to=o.promised_delivery_at or o.requested_delivery_at,
                partner=partner,
                items=tuple(items),
                note=o.notes,
            )
        )
    return result


def parse_statuses(raw: Iterable[str]) -> tuple[OrderStatus, ...]:
    """Parse status query values; unknown values are dropped.

    No valid value → the default set (everything but DRAFT/CANCELLED).
    """
    picked: list[OrderStatus] = []
    for value in raw:
        try:
            status = OrderStatus(value)
        except ValueError:
            continue
        if status not in picked:
            picked.append(status)
    return tuple(picked) if picked else DEFAULT_STATUSES
