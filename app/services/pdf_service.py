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

import re
from collections.abc import Mapping
from datetime import UTC, datetime, tzinfo
from decimal import ROUND_HALF_UP, Decimal
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID
from xml.sax.saxutils import escape as _xml_escape

import reportlab.rl_config
from babel import Locale, UnknownLocaleError
from babel.numbers import format_currency, format_decimal
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

# Imported as ``_t`` on purpose: Babel extracts ``_t:2`` (second argument
# is the msgid), which matches ``gettext(locale, msgid)``. Under any other
# alias the PDF labels were never extracted and printed in English on
# Czech documents ("Unit price", "Subtotal", the footer).
from app.i18n import gettext as _t
from app.models.enums import OrderStatus
from app.services.price_note import tenant_price_note
from app.timezones import format_local, tenant_tz

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


# ReportLab's Paragraph takes a mini-HTML dialect, so any user text
# interpolated into one is markup, not data. Two real consequences,
# both reproduced against the pinned reportlab:
#
#   * DoS — an order line described as `M8 <b>bolt, zinc` raises
#     "Parse error" and the whole PDF export 500s. Any customer contact
#     can type that (can_add_items defaults to True), and it breaks the
#     supplier's export, not just their own order.
#   * SSRF — `<img src="http://169.254.169.254/...">` makes the PDF
#     renderer fetch that URL server-side; rl_config trusts
#     file/http/https/ftp out of the box.
#
# Escape the VARIABLE, never the surrounding literal <b>/<i> tags.
def _esc(value: object) -> str:
    """XML-escape a value for safe interpolation into a Paragraph."""
    return _xml_escape("" if value is None else str(value))


# Defence in depth: even correctly escaped input cannot make the PDF
# engine open a socket or read a local file.
reportlab.rl_config.trustedSchemes = []
reportlab.rl_config.trustedHosts = []


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


# CLDR "currencySpacing": an alphabetic currency code that touches a
# digit gets a no-break space between them ("CZK 12,345.50"). Babel does
# not implement that rule and prints "CZK12,345.50" for ``en``; symbols
# such as ``$`` / ``€`` are left alone, as CLDR prescribes.
_CURRENCY_SPACING = re.compile(r"(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])")
_NBSP = "\u00a0"


def _babel_locale(locale: str | None) -> Locale:
    """Parse ``locale`` for number formatting, falling back to Czech."""
    try:
        return Locale.parse((locale or "cs").replace("-", "_"))
    except (UnknownLocaleError, ValueError, TypeError):
        return Locale.parse("cs")


def format_money(
    value: Decimal | float | int | None,
    currency: str | None = None,
    *,
    locale: str | None = "cs",
) -> str:
    """Format a monetary value per ``locale`` with exactly 2 decimals.

    ``cs`` → ``12 345,50 Kč``, ``de`` → ``12.345,50 CZK``,
    ``en`` → ``CZK 12,345.50`` (the gaps are U+00A0 no-break spaces, so
    an amount never wraps inside a narrow PDF cell; DejaVuSans has the
    glyph). Without ``currency`` only the number is printed. Unknown
    locales fall back to Czech, the document's legal language.

    Returns an empty string for ``None`` and non-finite values. Used by
    ``render_order_pdf`` for every price cell and total, and by
    ``invoice_pdf_service``.
    """
    if value is None:
        return ""
    dec = Decimal(value)
    if not dec.is_finite():
        return ""
    # Half-up, matching the stored line totals (LOGIC-17). Babel (like an
    # f-string ``:.2f``) rounds half-even: 0.125 would print as 0.12. We
    # round first, then let Babel only lay out an already-exact value.
    amount = dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    loc = _babel_locale(locale)
    if not currency:
        return format_decimal(amount, format="#,##0.00", locale=loc)
    text = format_currency(
        amount,
        currency.upper(),
        locale=loc,
        # Always two decimals, whatever CLDR says about the currency's
        # minor unit (the order columns are NUMERIC(…, 2)).
        currency_digits=False,
        decimal_quantization=False,
    )
    return _CURRENCY_SPACING.sub(_NBSP, text)


def _format_qty(value: Decimal | float | int | None, locale: str | None = "cs") -> str:
    """A quantity in the document's number format, like the UI's ``qty``
    filter: no trailing zeros, the locale's separators (``cs`` →
    ``37,5`` / ``1 200``, ``en`` → ``37.5`` / ``1,200``)."""
    if value is None:
        return ""
    dec = Decimal(value)
    if not dec.is_finite():
        return ""
    # Quantities are NUMERIC(12, 3): three decimals never round anything.
    text = format_decimal(dec, format="#,##0.###", locale=_babel_locale(locale))
    return text.replace(chr(0x202F), _NBSP)  # a narrow NBSP from CLDR -> NBSP


#: Same prettifying as the UI's ``_units.html`` macro: stored units stay
#: as typed ("m2"), only the rendering gets the superscript.
_PRETTY_UNITS = {
    "m2": "m²",
    "m3": "m³",
    "cm2": "cm²",
    "cm3": "cm³",
    "mm2": "mm²",
    "mm3": "mm³",
    "dm2": "dm²",
    "dm3": "dm³",
    "km2": "km²",
}


def pretty_unit(value: str | None) -> str:
    """``m2`` → ``m²`` (case-insensitive); anything else unchanged."""
    raw = (value or "").strip()
    return _PRETTY_UNITS.get(raw.lower(), raw)


#: The SKU cell's font size: a catalogue code is a reference, not content.
_SKU_FONT_SIZE = 8


def _sku_lines(sku: str, font: str, width: float) -> list[str]:
    """``sku`` split into lines that fit ``width`` — only after a hyphen.

    ReportLab breaks a long word anywhere ("LIS-MATIC / E", demo review
    P2-3); a code must stay readable, so it wraps at its own hyphens and
    a segment without one is never split.
    """
    parts = re.findall(r"[^-]+-?|-", sku)
    lines: list[str] = []
    for part in parts:
        if lines and pdfmetrics.stringWidth(lines[-1] + part, font, _SKU_FONT_SIZE) <= width:
            lines[-1] += part
        else:
            lines.append(part)
    return lines or [""]


def _sku_paragraph(sku: str, style: ParagraphStyle, width: float) -> Paragraph:
    """The SKU cell: hyphen-wrapped lines, the font shrunk (not the code
    split) when a hyphen-free segment is still wider than the cell."""
    lines = _sku_lines(sku, style.fontName, width)
    widest = max(pdfmetrics.stringWidth(line, style.fontName, _SKU_FONT_SIZE) for line in lines)
    size = float(_SKU_FONT_SIZE)
    if widest > width:
        size = max(5.5, size * width / widest)
    cell_style = ParagraphStyle(
        "sku", parent=style, fontSize=size, leading=size * 1.25, splitLongWords=0
    )
    return Paragraph("<br/>".join(_esc(line) for line in lines), cell_style)


#: Same day-first formats as the web UI (``localdate`` / ``localtime``
#: filters), so the PDF and the page a customer compares it with agree
#: (demo review P2-3).
DATE_FORMAT = "%d.%m.%Y"
DATETIME_FORMAT = "%d.%m.%Y %H:%M"


def _format_date(value) -> str:
    if value is None:
        return ""
    # Accept both date and datetime.
    if hasattr(value, "strftime"):
        return value.strftime(DATE_FORMAT)
    return str(value)


def _format_datetime(value, tz: tzinfo | None = None, fmt: str = DATETIME_FORMAT) -> str:
    """A stored UTC instant as wall-clock time in the tenant's zone ``tz``."""
    if value is None:
        return ""
    if hasattr(value, "strftime"):
        return format_local(value, tz, fmt)
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
    skus: Mapping[UUID, str] | None = None,
) -> bytes:
    """Render a single order to PDF and return the raw bytes.

    The layout is one-pass A4 with ~20mm margins; large item lists page
    break naturally because ``Table`` is a flowable. No network I/O, no
    disk writes — the whole document lives in an in-memory buffer.

    ``locale`` is the resolved request locale; defaults to ``cs`` so the
    function is safe to call from contexts that don't yet have one.

    ``skus`` maps ``product_id`` → the catalog product's SKU for the
    "SKU" column; a free-text line (or a product no longer in the map)
    prints an empty cell. The tenant's price note
    (:mod:`app.services.price_note`) is printed under the totals.
    """
    font, font_bold = _register_fonts()
    # Every timestamp on the document is the tenant's local time (E2).
    tz = tenant_tz(tenant)

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
        title=f"{_t(locale, 'Order')} {order.number}",
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
    story.append(Paragraph(_esc(tenant_name), h1))
    story.append(Spacer(1, 6))
    story.append(
        Paragraph(
            f"{_t(locale, 'Order')} <b>{_esc(order.number)}</b>",
            h2,
        )
    )
    # The order's own name, the line a person recognises it by (P2-3).
    if (order.title or "").strip():
        story.append(Paragraph(_esc(order.title.strip()), normal))
    story.append(Spacer(1, 10))

    # ------------------------------------------------ Meta block
    status_label = _t(locale, _STATUS_LABELS.get(order.status, order.status.value))
    meta_rows = [
        [
            Paragraph(f"<b>{_t(locale, 'Status')}:</b>", normal),
            Paragraph(status_label, normal),
            Paragraph(f"<b>{_t(locale, 'Created')}:</b>", normal),
            Paragraph(_format_datetime(order.created_at, tz), normal),
        ],
        [
            Paragraph(f"<b>{_t(locale, 'Submitted')}:</b>", normal),
            Paragraph(_format_datetime(order.submitted_at, tz), normal),
            Paragraph(f"<b>{_t(locale, 'Promised delivery')}:</b>", normal),
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
        story.append(Paragraph(_t(locale, "Customer"), h2))
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
            cust_lines.append(f"{_t(locale, 'Company ID')}: {customer.ico}")
        if customer.dic:
            cust_lines.append(f"{_t(locale, 'Tax ID')}: {customer.dic}")
        for line in cust_lines:
            story.append(Paragraph(_esc(line), normal))
        story.append(Spacer(1, 12))

    # ------------------------------------------------ Items table
    story.append(Paragraph(_t(locale, "Items"), h2))

    # Money columns are sized so a locale-formatted amount up to
    # 99 999 999,99 fits on one line: the no-break spaces keep it from
    # wrapping between digit groups, so a too-narrow cell would chop the
    # number itself in two. The SKU column fits the usual job-shop code
    # ("MAT-1.4301-2") on one line at 8 pt and wraps longer ones only at
    # a hyphen (demo review P2-3).
    col_widths = [27 * mm, 45 * mm, 25 * mm, 39 * mm, 39 * mm]
    cell_padding = 6  # ReportLab's default LEFT/RIGHTPADDING
    sku_width = col_widths[0] - 2 * cell_padding
    header = [
        Paragraph(_t(locale, "SKU"), th),
        Paragraph(_t(locale, "Name"), th),
        Paragraph(_t(locale, "Quantity"), th),
        # "Cena/j." — the price is per the line's unit (ks, m, kg, hod),
        # not "per piece" (P3-6, as in the web UI).
        Paragraph(_t(locale, "Price per unit"), th),
        Paragraph(_t(locale, "Line total"), th),
    ]
    data: list[list] = [header]

    subtotal = Decimal("0")
    for item in items:
        line_total = item.line_total
        if line_total is None and item.unit_price is not None:
            line_total = (Decimal(item.unit_price) * Decimal(item.quantity)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        if line_total is not None and Decimal(line_total).is_finite():
            subtotal += Decimal(line_total)

        # SKU from the linked catalog product (P2-3); free-text lines have
        # no product and print an empty cell. A description that repeats
        # the SKU as a "<sku> — " prefix is not printed twice.
        product_id = getattr(item, "product_id", None)
        sku = (skus or {}).get(product_id, "") if product_id is not None else ""
        name = item.description or ""
        if sku and name.startswith(f"{sku} — "):
            name = name[len(sku) + 3 :]

        qty_str = f"{_format_qty(item.quantity, locale)} {pretty_unit(item.unit)}".strip()
        data.append(
            [
                _sku_paragraph(sku, normal, sku_width),
                Paragraph(_esc(name), normal),
                Paragraph(_esc(qty_str), normal),
                Paragraph(format_money(item.unit_price, order.currency, locale=locale), normal),
                Paragraph(format_money(line_total, order.currency, locale=locale), normal),
            ]
        )

    items_table = Table(data, colWidths=col_widths, repeatRows=1)
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
            Paragraph(f"<b>{_t(locale, 'Subtotal')}</b>", th),
            Paragraph(format_money(total_value, order.currency, locale=locale), th),
        ],
    ]
    # The agreed amount, once there is one (LOGIC-2/17). It is the
    # snapshot taken at confirmation, so a later correction to the lines
    # shows up as a visible difference rather than silently rewriting
    # what the customer accepted. Nothing is printed about VAT: no tenant
    # setting records whether prices are net or gross, and guessing would
    # put a false statement on a commercial document — the tenant's own
    # price note (below the totals) is where that sentence belongs.
    confirmed_total = getattr(order, "confirmed_total", None)
    confirmed_at = getattr(order, "confirmed_at", None)
    if confirmed_at is not None and confirmed_total is not None:
        totals_rows.append(
            [
                Paragraph(
                    f"<b>{_t(locale, 'Confirmed total')}</b> "
                    f"({_esc(_format_datetime(confirmed_at, tz))})",
                    th,
                ),
                Paragraph(format_money(confirmed_total, order.currency, locale=locale), th),
            ]
        )
    totals_table = Table(totals_rows, colWidths=[130 * mm, 45 * mm])
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

    # ------------------------------------------------ Price note
    # The tenant's own sentence about the price basis (VAT, payment
    # terms). Printed only when set: the portal does not know whether the
    # supplier is a VAT payer and must not guess on a commercial document.
    price_note = tenant_price_note(tenant)
    if price_note:
        story.append(Spacer(1, 10))
        story.append(Paragraph(_esc(price_note).replace("\n", "<br/>"), normal))

    # ------------------------------------------------ Build (with footer)

    def _on_page(canvas, _doc) -> None:
        """Draw a footer on every page — generated_at + disclaimer."""
        canvas.saveState()
        canvas.setFont(font, 8)
        canvas.setFillGray(0.4)
        footer_text = "{} — {} · {}".format(
            _t(locale, "Generated"),
            # Zone abbreviation (CET / CEST) — a printed page has no
            # other way to say which clock it was generated by.
            _format_datetime(datetime.now(UTC), tz, f"{DATETIME_FORMAT} %Z"),
            _t(
                locale,
                "This document is for informational purposes only.",
            ),
        )
        canvas.drawString(20 * mm, 10 * mm, footer_text)
        # Page number on the right.
        canvas.drawRightString(
            A4[0] - 20 * mm,
            10 * mm,
            f"{_t(locale, 'Page')} {_doc.page}",
        )
        canvas.restoreState()

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    return buffer.getvalue()
