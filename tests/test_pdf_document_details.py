"""Order PDF details (demo review P2-3): SKU from the product, day-first
dates, the order title, and the tenant's price note."""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.customer import Customer
from app.models.enums import OrderStatus
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.tenant import Tenant
from app.services.pdf_service import render_order_pdf
from app.services.price_note import normalize_price_note, tenant_price_note

needs_pdftotext = pytest.mark.skipif(
    shutil.which("pdftotext") is None, reason="poppler-utils not installed"
)


def _text(pdf: bytes) -> str:
    return subprocess.run(
        ["pdftotext", "-layout", "-", "-"], input=pdf, capture_output=True, check=True
    ).stdout.decode()


def _fixture(settings: dict | None = None):
    tenant = Tenant(
        id=uuid4(),
        slug="4mex",
        name="4MEX s.r.o.",
        billing_email="b@b.cz",
        storage_prefix="tenants/4mex/",
        settings=settings or {},
    )
    customer = Customer(id=uuid4(), tenant_id=tenant.id, name="ACME")
    order = Order(
        id=uuid4(),
        tenant_id=tenant.id,
        customer_id=customer.id,
        number="2026-000042",
        title="Kryty K-07 pro linku 3",
        status=OrderStatus.CONFIRMED,
        quoted_total=Decimal("1200.00"),
        currency="CZK",
    )
    order.created_at = datetime(2026, 10, 1, 8, 15, tzinfo=UTC)
    order.submitted_at = datetime(2026, 10, 1, 8, 15, tzinfo=UTC)
    order.promised_delivery_at = date(2026, 10, 18)
    product_id = uuid4()
    items = [
        OrderItem(
            id=uuid4(),
            tenant_id=tenant.id,
            order_id=order.id,
            position=0,
            product_id=product_id,
            description="KRYT-K07 — Kryt K-07",
            quantity=Decimal("10"),
            unit="ks",
            unit_price=Decimal("100"),
            line_total=Decimal("1000.00"),
        ),
        OrderItem(
            id=uuid4(),
            tenant_id=tenant.id,
            order_id=order.id,
            position=1,
            description="Doprava — Brno",
            quantity=Decimal("1"),
            unit="ks",
            unit_price=Decimal("200"),
            line_total=Decimal("200.00"),
        ),
    ]
    return order, items, customer, tenant, product_id


@needs_pdftotext
def test_sku_comes_from_the_linked_product() -> None:
    order, items, customer, tenant, product_id = _fixture()
    text = _text(
        render_order_pdf(order, items, customer, tenant, locale="cs", skus={product_id: "KRYT-K07"})
    )
    line = next(ln for ln in text.splitlines() if "Kryt K-07" in ln)
    assert line.strip().startswith("KRYT-K07")
    # The "<sku> — " prefix is not printed twice.
    assert line.count("KRYT-K07") == 1
    # A free-text line keeps its whole description and an empty SKU cell;
    # the old "split on the dash" guess no longer invents a code.
    transport = next(ln for ln in text.splitlines() if "Brno" in ln)
    assert "Doprava — Brno" in transport


@needs_pdftotext
def test_without_sku_map_the_cell_is_empty() -> None:
    order, items, customer, tenant, _ = _fixture()
    text = _text(render_order_pdf(order, items, customer, tenant, locale="cs"))
    line = next(ln for ln in text.splitlines() if "Kryt K-07" in ln)
    assert line.strip().startswith("KRYT-K07 — Kryt K-07")


@needs_pdftotext
@pytest.mark.parametrize("locale", ["cs", "de", "en"])
def test_dates_are_day_first_like_the_ui_and_title_is_shown(locale) -> None:
    order, items, customer, tenant, _ = _fixture()
    text = _text(render_order_pdf(order, items, customer, tenant, locale=locale))
    assert "18.10.2026" in text  # promised delivery (a plain date)
    assert "01.10.2026 10:15" in text  # created, Prague time
    assert "2026-10-18" not in text
    assert "Kryty K-07 pro linku 3" in text
    # The title sits right under the order number.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    number_at = next(i for i, ln in enumerate(lines) if "2026-000042" in ln)
    assert lines[number_at + 1] == "Kryty K-07 pro linku 3"


@needs_pdftotext
def test_price_note_is_printed_only_when_set() -> None:
    order, items, customer, tenant, _ = _fixture()
    assert "DPH" not in _text(render_order_pdf(order, items, customer, tenant, locale="cs"))

    order, items, customer, tenant, _ = _fixture(
        {"price_note": "Ceny jsou uvedeny bez DPH.\nSplatnost 14 dní."}
    )
    text = _text(render_order_pdf(order, items, customer, tenant, locale="cs"))
    assert "Ceny jsou uvedeny bez DPH." in text
    assert "Splatnost 14 dní." in text


def test_price_note_normalisation() -> None:
    assert normalize_price_note(None) == ""
    assert normalize_price_note("  a \r\n\r\n b  ") == "a\nb"
    assert normalize_price_note("1\n2\n3\n4\n5") == "1\n2\n3\n4 5"
    assert tenant_price_note(None) == ""
    assert tenant_price_note(Tenant(settings={"price_note": " x "})) == "x"
    assert tenant_price_note(Tenant(settings={"price_note": 5})) == ""


# ------------------------------------------------------------- Postgres


@pytest.mark.postgres
async def test_price_note_setting_round_trip_and_pdf_route(
    tenant_client, owner_engine, demo_tenant
) -> None:
    from tests.test_orders_item_autosave import _login, _seed

    seed = await _seed(owner_engine, demo_tenant.id)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        product = Product(
            id=uuid4(), tenant_id=demo_tenant.id, sku="KM-120", name="Konzole", unit="ks"
        )
        order = Order(
            id=uuid4(),
            tenant_id=demo_tenant.id,
            customer_id=seed["acme"].id,
            number="2026-000077",
            title="Konzole pro halu B",
            status=OrderStatus.SUBMITTED,
            currency="CZK",
        )
        session.add_all([product, order])
        await session.flush()
        session.add(
            OrderItem(
                tenant_id=demo_tenant.id,
                order_id=order.id,
                position=0,
                product_id=product.id,
                description="Konzole",
                quantity=Decimal("4"),
                unit="ks",
            )
        )
        order_id = order.id

    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    page = (await tenant_client.get("/app/admin/tenant-settings")).text
    assert 'name="price_note"' in page

    resp = await tenant_client.post(
        "/app/admin/tenant-settings",
        data={
            "default_locale": "cs",
            "timezone": "Europe/Prague",
            "price_note": "  Nejsme plátci DPH.  ",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    async with sm() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.id == demo_tenant.id))
        ).scalar_one()
        assert tenant.settings["price_note"] == "Nejsme plátci DPH."
    assert "Nejsme plátci DPH." in (await tenant_client.get("/app/admin/tenant-settings")).text

    too_long = await tenant_client.post(
        "/app/admin/tenant-settings",
        data={"default_locale": "cs", "timezone": "Europe/Prague", "price_note": "x" * 501},
    )
    assert too_long.status_code == 400

    pdf = await tenant_client.get(f"/app/orders/{order_id}.pdf")
    assert pdf.status_code == 200
    if shutil.which("pdftotext"):
        text = _text(pdf.content)
        assert "KM-120" in text
        assert "Nejsme plátci DPH." in text
        assert "Konzole pro halu B" in text

    # Clearing the field removes the key.
    await tenant_client.post(
        "/app/admin/tenant-settings",
        data={"default_locale": "cs", "timezone": "Europe/Prague", "price_note": ""},
    )
    async with sm() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.id == demo_tenant.id))
        ).scalar_one()
        assert "price_note" not in tenant.settings


# ------------------------------------------- round 2: SKU, quantity, header


def _catalogue_items(order: Order, tenant: Tenant) -> tuple[list[OrderItem], dict]:
    """One line per demo-seed SKU, plus a decimal m2 line."""
    from app.demo.seed import PRODUCTS

    items, skus = [], {}
    for pos, (sku, (name, unit, price, _client)) in enumerate(PRODUCTS.items()):
        pid = uuid4()
        skus[pid] = sku
        items.append(
            OrderItem(
                id=uuid4(),
                tenant_id=tenant.id,
                order_id=order.id,
                position=pos,
                product_id=pid,
                description=name,
                quantity=Decimal("2"),
                unit=unit,
                unit_price=Decimal(price),
                line_total=Decimal(price) * 2,
            )
        )
    items.append(
        OrderItem(
            id=uuid4(),
            tenant_id=tenant.id,
            order_id=order.id,
            position=len(items),
            description="Lakování",
            quantity=Decimal("37.500"),
            unit="m2",
            unit_price=Decimal("260"),
            line_total=Decimal("9750.00"),
        )
    )
    return items, skus


@needs_pdftotext
def test_every_seed_sku_prints_whole() -> None:
    """P2-3: "LIS-MATIC / E" — the code column split SKUs mid-word."""
    from app.demo.seed import PRODUCTS

    order, _items, customer, tenant, _ = _fixture()
    items, skus = _catalogue_items(order, tenant)
    text = _text(render_order_pdf(order, items, customer, tenant, locale="cs", skus=skus))
    first_cells = [ln.split()[0] for ln in text.splitlines() if ln.strip()]
    for sku in PRODUCTS:
        assert sku in first_cells, sku


def test_a_long_sku_wraps_only_at_hyphens() -> None:
    from reportlab.lib.units import mm

    from app.services.pdf_service import _register_fonts, _sku_lines

    font, _bold = _register_fonts()
    lines = _sku_lines("VERY-LONG-SKU-CODE-12345", font, 15 * mm)
    assert "".join(lines) == "VERY-LONG-SKU-CODE-12345"
    assert all(line.endswith("-") for line in lines[:-1])
    assert len(lines) > 1
    assert _sku_lines("KONTROLA", font, 15 * mm) == ["KONTROLA"]


@needs_pdftotext
@pytest.mark.parametrize(
    ("locale", "qty", "header"),
    [("cs", "37,5 m²", "Cena/j."), ("en", "37.5 m²", "Price per unit"), ("de", "37,5 m²", None)],
)
def test_quantities_and_price_header_match_the_ui(locale: str, qty: str, header) -> None:
    """P2-3: quantities in the document's number format with m² like the
    UI, and the price column is per unit, not "Cena/ks"."""
    order, _items, customer, tenant, _ = _fixture()
    items, skus = _catalogue_items(order, tenant)
    text = _text(render_order_pdf(order, items, customer, tenant, locale=locale, skus=skus))
    flat = text.replace(chr(0xA0), " ")
    assert qty in flat
    assert "37.500" not in flat and "m2" not in flat
    assert "Cena/ks" not in flat and "Unit price" not in flat
    if header:
        assert header in flat
