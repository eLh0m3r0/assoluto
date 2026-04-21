"""PDF generation for order exports.

Uses ReportLab's Platypus layout engine on an in-memory ``BytesIO`` so we
can stream the bytes back through a FastAPI response without touching disk.

Font story
----------
ReportLab ships a short list of built-in Type 1 fonts (Helvetica, Times,
Courier) whose encodings do not cover Czech diacritics such as ``č ř š ž``.
The portal is Czech-first, so we bundle DejaVuSans regular + bold TrueType
files under ``app/static/fonts/`` and register them on first use. The TTFs
come from the upstream dejavu-fonts project (public-domain-ish; Bitstream
Vera License + the specific Arev License amendment — see the ``LICENSE``
file next to the fonts for details).

If the TTF files are ever missing at runtime we fall back to Helvetica so
the feature degrades gracefully — diacritics may render as tofu but the
document still generates. Don't rely on that path for real users.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFError, TTFont
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.i18n import gettext as _gettext
from app.models.enums import OrderStatus

if TYPE_CHECKING:  # pragma: no cover - type hints only
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.tenant import Tenant


# --------------------------------------------------------------------------
# Fonts
# --------------------------------------------------------------------------

FONTS_DIR = Path(__file__).resolve().parent.parent / "static" / "fonts"
_FONT_NAME = "DejaVuSans"
_FONT_NAME_BOLD = "DejaVuSans-Bold"
_FONTS_REGISTERED = False


def _register_fonts() -> tuple[str, str]:
    """Register DejaVuSans fonts once per process; return (regular, bold).

    Returns ``("Helvetica", "Helvetica-Bold")`` if the TTF files are
    missing — in practice they ship with the repo, so the fallback is for
    exotic deployments only. Diacritics will not render in the fallback.
    """
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return _FONT_NAME, _FONT_NAME_BOLD

    regular = FONTS_DIR / "DejaVuSans.ttf"
    bold = FONTS_DIR / "DejaVuSans-Bold.ttf"
    if not regular.exists() or not bold.exists():
        # TODO: remove fallback once CI guarantees fonts are bundled.
        return "Helvetica", "Helvetica-Bold"

    try:
        pdfmetrics.registerFont(TTFont(_FONT_NAME, str(regular)))
        pdfmetrics.registerFont(TTFont(_FONT_NAME_BOLD, str(bold)))
    except TTFError:  # pragma: no cover - defensive
        return "Helvetica", "Helvetica-Bold"

    _FONTS_REGISTERED = True
    return _FONT_NAME, _FONT_NAME_BOLD


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


# Human-readable labels for each order status. English strings are the
# gettext message IDs; the real localisation is looked up by ``_gettext``.
_STATUS_LABELS: dict[OrderStatus, str] = {
    OrderStatus.DRAFT: "Draft",
    OrderStatus.SUBMITTED: "Submitted",
    OrderStatus.QUOTED: "Quoted",
    OrderStatus.CONFIRMED: "Confirmed",
    OrderStatus.IN_PRODUCTION: "In production",
    OrderStatus.READY: "Ready",
    OrderStatus.DELIVERED: "Delivered",
    OrderStatus.CLOSED: "Closed",
    OrderStatus.CANCELLED: "Cancelled",
}


def format_money(value: Decimal | float | int | None, currency: str | None = None) -> str:
    """Format a monetary value with 2 decimals and optional currency suffix.

    Returns an empty string for ``None``. Used by ``render_order_pdf`` for
    every price cell and total; also handy for other PDF variants that may
    share this module later.
    """
    if value is None:
        return ""
    amount = f"{Decimal(value):.2f}"
    if currency:
        return f"{amount} {currency}"
    return amount


def _format_qty(value: Decimal | float | int | None) -> str:
    """Format a quantity. Strips trailing zeros so ``1.000`` → ``1``."""
    if value is None:
        return ""
    d = Decimal(value).normalize()
    # ``normalize()`` returns scientific form for large ints; quantize back.
    as_str = format(d, "f")
    return as_str


def _format_date(value) -> str:
    if value is None:
        return ""
    # Accept both date and datetime.
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value)


def _format_datetime(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------


def render_order_pdf(
    order: Order,
    items: list[OrderItem],
    customer: Customer | None,
    tenant: Tenant | None,
    *,
    locale: str = "cs",
) -> bytes:
    """Render a single order to PDF and return the raw bytes.

    The layout is one-pass A4 with ~20mm margins; large item lists page
    break naturally because ``Table`` is a flowable. No network I/O, no
    disk writes — the whole document lives in an in-memory buffer.

    ``locale`` is the resolved request locale; defaults to ``cs`` so the
    function is safe to call from contexts that don't yet have one.
    """
    font, font_bold = _register_fonts()

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
        title=f"{_gettext(locale, 'Order')} {order.number}",
        author=tenant.name if tenant else "",
    )

    # Stylesheet — start from the sample sheet but swap every font to our
    # DejaVu so Czech diacritics render correctly.
    sheet = getSampleStyleSheet()
    for style in sheet.byName.values():
        if hasattr(style, "fontName"):
            style.fontName = font_bold if "Bold" in (style.fontName or "") else font
    h1 = ParagraphStyle("h1", parent=sheet["Title"], fontName=font_bold, fontSize=18, leading=22)
    h2 = ParagraphStyle("h2", parent=sheet["Heading2"], fontName=font_bold, fontSize=12, leading=16)
    normal = ParagraphStyle(
        "normal", parent=sheet["Normal"], fontName=font, fontSize=10, leading=13
    )
    th = ParagraphStyle("th", parent=normal, fontName=font_bold, fontSize=10, leading=12)

    story: list = []

    # ------------------------------------------------ Header
    tenant_name = tenant.name if tenant else ""
    story.append(Paragraph(tenant_name, h1))
    story.append(Spacer(1, 6))
    story.append(
        Paragraph(
            f"{_gettext(locale, 'Order')} <b>{order.number}</b>",
            h2,
        )
    )
    story.append(Spacer(1, 10))

    # ------------------------------------------------ Meta block
    status_label = _gettext(locale, _STATUS_LABELS.get(order.status, order.status.value))
    meta_rows = [
        [
            Paragraph(f"<b>{_gettext(locale, 'Status')}:</b>", normal),
            Paragraph(status_label, normal),
            Paragraph(f"<b>{_gettext(locale, 'Created')}:</b>", normal),
            Paragraph(_format_datetime(order.created_at), normal),
        ],
        [
            Paragraph(f"<b>{_gettext(locale, 'Submitted')}:</b>", normal),
            Paragraph(_format_datetime(order.submitted_at), normal),
            Paragraph(f"<b>{_gettext(locale, 'Promised delivery')}:</b>", normal),
            Paragraph(_format_date(order.promised_delivery_at), normal),
        ],
    ]
    meta_table = Table(meta_rows, colWidths=[35 * mm, 50 * mm, 40 * mm, 40 * mm])
    meta_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    story.append(meta_table)
    story.append(Spacer(1, 12))

    # ------------------------------------------------ Customer block
    if customer is not None:
        story.append(Paragraph(_gettext(locale, "Customer"), h2))
        cust_lines = [customer.name]
        # billing_address is a JSON blob with free-form keys. Render a few
        # conventional ones if present, skip otherwise.
        addr = customer.billing_address or {}
        if isinstance(addr, dict):
            street = addr.get("street") or addr.get("line1")
            city = addr.get("city")
            zip_code = addr.get("zip") or addr.get("postal_code")
            country = addr.get("country")
            if street:
                cust_lines.append(str(street))
            if city or zip_code:
                cust_lines.append(" ".join(x for x in [str(zip_code or ""), str(city or "")] if x))
            if country:
                cust_lines.append(str(country))
        if customer.ico:
            cust_lines.append(f"{_gettext(locale, 'Company ID')}: {customer.ico}")
        if customer.dic:
            cust_lines.append(f"{_gettext(locale, 'Tax ID')}: {customer.dic}")
        for line in cust_lines:
            story.append(Paragraph(line, normal))
        story.append(Spacer(1, 12))

    # ------------------------------------------------ Items table
    story.append(Paragraph(_gettext(locale, "Items"), h2))

    header = [
        Paragraph(_gettext(locale, "SKU"), th),
        Paragraph(_gettext(locale, "Name"), th),
        Paragraph(_gettext(locale, "Quantity"), th),
        Paragraph(_gettext(locale, "Unit price"), th),
        Paragraph(_gettext(locale, "Line total"), th),
    ]
    data: list[list] = [header]

    subtotal = Decimal("0")
    for item in items:
        line_total = item.line_total
        if line_total is None and item.unit_price is not None:
            line_total = Decimal(item.unit_price) * Decimal(item.quantity)
        if line_total is not None:
            subtotal += Decimal(line_total)

        # SKU: free-text items have no product_id; leave blank.
        sku = ""
        # ``OrderItem`` has no denormalised SKU column, so we simply omit
        # it unless the description happens to start with the pattern
        # ``"<sku> — <name>"`` (which is the convention for product-picked
        # items set in orders.py:add_item).
        desc = item.description or ""
        name = desc
        if " — " in desc:
            maybe_sku, rest = desc.split(" — ", 1)
            # Heuristic: SKUs are short, no spaces.
            if maybe_sku and " " not in maybe_sku and len(maybe_sku) <= 40:
                sku = maybe_sku
                name = rest

        qty_str = f"{_format_qty(item.quantity)} {item.unit or ''}".strip()
        data.append(
            [
                Paragraph(sku, normal),
                Paragraph(name, normal),
                Paragraph(qty_str, normal),
                Paragraph(format_money(item.unit_price, order.currency), normal),
                Paragraph(format_money(line_total, order.currency), normal),
            ]
        )

    items_table = Table(
        data,
        colWidths=[25 * mm, 75 * mm, 25 * mm, 25 * mm, 25 * mm],
        repeatRows=1,
    )
    items_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.grey),
                ("LINEBELOW", (0, -1), (-1, -1), 0.25, colors.lightgrey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("ALIGN", (2, 1), (4, -1), "RIGHT"),
            ]
        )
    )
    story.append(items_table)
    story.append(Spacer(1, 10))

    # ------------------------------------------------ Totals
    # Use the stored quoted_total when present (staff-priced), otherwise
    # fall back to the subtotal computed from the line items. No tax field
    # exists on the model, so we only report one line.
    total_value = Decimal(order.quoted_total) if order.quoted_total is not None else subtotal
    totals_rows = [
        [
            Paragraph(f"<b>{_gettext(locale, 'Subtotal')}</b>", th),
            Paragraph(format_money(total_value, order.currency), th),
        ],
    ]
    totals_table = Table(totals_rows, colWidths=[140 * mm, 35 * mm])
    totals_table.setStyle(
        TableStyle(
            [
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(totals_table)

    # ------------------------------------------------ Build (with footer)

    def _on_page(canvas, _doc) -> None:
        """Draw a footer on every page — generated_at + disclaimer."""
        canvas.saveState()
        canvas.setFont(font, 8)
        canvas.setFillGray(0.4)
        footer_text = "{} — {} · {}".format(
            _gettext(locale, "Generated"),
            datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            _gettext(
                locale,
                "This document is for informational purposes only.",
            ),
        )
        canvas.drawString(20 * mm, 10 * mm, footer_text)
        # Page number on the right.
        canvas.drawRightString(
            A4[0] - 20 * mm,
            10 * mm,
            f"{_gettext(locale, 'Page')} {_doc.page}",
        )
        canvas.restoreState()

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    return buffer.getvalue()
