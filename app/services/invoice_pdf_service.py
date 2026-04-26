"""Czech-tax-compliant invoice PDFs for platform subscriptions.

Stripe's hosted invoice is fine for B2C / international B2B but does
not carry the fields a Czech tax authority expects on a
**daňový doklad** (tax document) under §29 of Zákon 235/2004 Sb.
(VAT Act). In particular:

* Designation as ``Daňový doklad`` (tax document),
* Supplier IČO **and** DIČ if VAT-registered,
* Customer IČO + DIČ (when B2B),
* *Datum uskutečnění zdanitelného plnění* (date of taxable supply;
  "DUZP") separate from the issue date,
* Per-rate VAT breakdown in CZK (when applicable).

This service renders a PDF that we can attach to Stripe's email or
offer as a direct download from ``/platform/billing``. The layout
uses the same DejaVuSans font registered by ``pdf_service`` so Czech
diacritics render correctly.

Non-VAT payers (empty ``PLATFORM_OPERATOR_DIC``) get a simpler
"Faktura" (non-tax invoice) header and no DPH rows — legal under
§11 VAT Act for operators below the registration threshold.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from io import BytesIO
from typing import TYPE_CHECKING

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.services.pdf_service import _register_fonts

if TYPE_CHECKING:  # pragma: no cover
    from app.config import Settings
    from app.models.tenant import Tenant
    from app.platform.billing.models import Invoice


# Czech standard VAT rate. Reduced rates (15%, 12%) only apply to
# specific goods lists; SaaS service falls under the standard rate.
CZ_VAT_STANDARD = Decimal("0.21")


def _dot_amount(amount: Decimal) -> str:
    """Format an amount with Czech convention: comma decimal + space
    thousand-separator (``1 234,56``)."""
    q = amount.quantize(Decimal("0.01"))
    whole, _, frac = f"{q:.2f}".partition(".")
    whole_grouped = f"{int(whole):,}".replace(",", " ")  # noqa: RUF001
    return f"{whole_grouped},{frac}"


# Per-locale label set for the invoice PDF. CS is the source of truth
# (matches the live document under §29 Zákon 235/2004 Sb.); EN/DE are
# courtesy translations for foreign Customer Contacts of the operator.
# IČO/DIČ stay in their CS form everywhere — they are Czech regulatory
# identifiers, not generic phrases.
_INVOICE_LABELS: dict[str, dict[str, str]] = {
    "cs": {
        "doc_label_vat": "Daňový doklad – faktura",  # noqa: RUF001
        "doc_label_nonvat": "Faktura",
        "doc_number": "Číslo dokladu",
        "issue_date": "Datum vystavení",
        "duzp": "Datum zdanitelného plnění (DUZP)",
        "supplier": "Dodavatel",
        "customer": "Odběratel",
        "address_not_configured": "&lt;adresa nenakonfigurována&gt;",
        "not_configured": "&lt;nenakonfigurováno&gt;",
        "non_vat_payer": "Neplátce DPH (§6 ZDPH)",
        "col_description": "Popis",
        "col_base": "Cena bez DPH",
        "col_rate": "Sazba",
        "col_vat": "DPH",
        "col_total": "Celkem",
        "subscription_line": "Předplatné Assoluto – měsíc",  # noqa: RUF001
        "vat_base": "Základ DPH",
        "vat_label": "DPH",
        "total_due": "Celkem k úhradě",
        "paid_via_stripe": "Uhrazeno {date} – přes Stripe.",  # noqa: RUF001
        "not_yet_paid": "Úhrada nebyla dosud zaznamenána.",
        "electronic_notice": "Doklad byl vystaven elektronicky a je platný bez razítka a podpisu.",
        "country": "Česká republika",
    },
    "en": {
        "doc_label_vat": "Tax invoice (daňový doklad)",
        "doc_label_nonvat": "Invoice (faktura)",
        "doc_number": "Document number",
        "issue_date": "Issue date",
        "duzp": "Date of taxable supply (DUZP)",
        "supplier": "Supplier",
        "customer": "Customer",
        "address_not_configured": "&lt;address not configured&gt;",
        "not_configured": "&lt;not configured&gt;",
        "non_vat_payer": "Not a VAT payer (§6 VAT Act)",
        "col_description": "Description",
        "col_base": "Price excl. VAT",
        "col_rate": "Rate",
        "col_vat": "VAT",
        "col_total": "Total",
        "subscription_line": "Assoluto subscription – month",  # noqa: RUF001
        "vat_base": "VAT base",
        "vat_label": "VAT",
        "total_due": "Total due",
        "paid_via_stripe": "Paid on {date} via Stripe.",
        "not_yet_paid": "Payment not yet recorded.",
        "electronic_notice": "This document was issued electronically and is valid without a signature or stamp.",
        "country": "Czech Republic",
    },
    "de": {
        "doc_label_vat": "Steuerbeleg (daňový doklad)",
        "doc_label_nonvat": "Rechnung (faktura)",
        "doc_number": "Belegnummer",
        "issue_date": "Ausstellungsdatum",
        "duzp": "Datum der steuerbaren Leistung (DUZP)",
        "supplier": "Lieferant",
        "customer": "Empfänger",
        "address_not_configured": "&lt;Adresse nicht konfiguriert&gt;",
        "not_configured": "&lt;nicht konfiguriert&gt;",
        "non_vat_payer": "Kein USt-Zahler (§6 ZDPH)",
        "col_description": "Beschreibung",
        "col_base": "Preis ohne USt",
        "col_rate": "Satz",
        "col_vat": "USt",
        "col_total": "Gesamt",
        "subscription_line": "Assoluto-Abonnement – Monat",  # noqa: RUF001
        "vat_base": "USt-Bemessungsgrundlage",
        "vat_label": "USt",
        "total_due": "Zu zahlen",
        "paid_via_stripe": "Bezahlt am {date} – via Stripe.",  # noqa: RUF001
        "not_yet_paid": "Zahlung wurde noch nicht erfasst.",
        "electronic_notice": "Der Beleg wurde elektronisch ausgestellt und ist ohne Stempel und Unterschrift gültig.",
        "country": "Tschechische Republik",
    },
}


# Phrases that indicate the operator already added the country to
# ``platform_operator_address`` so we don't append it twice. Lower-cased
# substring match.
_COUNTRY_PRESENT_HINTS = (
    "česká republika",
    "ceska republika",
    "czech republic",
    "tschechische republik",
    "czechia",
)


def _address_with_country(address: str, country_label: str) -> str:
    """Append the country to ``address`` (separated by `<br/>`) unless
    the address already mentions any of the localised country names.

    The supplier is always Czech-based, so the country is fixed in the
    catalog; we only translate the *label*. Operator-edited addresses
    that already contain the country (in any of the supported
    spellings) pass through unchanged."""
    rendered = address.replace("\n", "<br/>")
    haystack = address.lower()
    if any(hint in haystack for hint in _COUNTRY_PRESENT_HINTS):
        return rendered
    return f"{rendered}<br/>{country_label}"


def _labels_for(locale: str | None) -> dict[str, str]:
    """Return the label set for ``locale``, falling back to CS when the
    locale is unknown — the legally-binding document is the CS one."""
    if locale and locale in _INVOICE_LABELS:
        return _INVOICE_LABELS[locale]
    return _INVOICE_LABELS["cs"]


def render_invoice_pdf(
    *,
    invoice: Invoice,
    tenant: Tenant,
    settings: Settings,
    locale: str | None = None,
) -> bytes:
    """Render a single invoice into PDF bytes.

    ``tenant.settings`` may carry overrides for customer-side IČO/DIČ
    collected during signup (keys ``billing_ico`` / ``billing_dic``);
    falls back to the Stripe-held metadata if the webhook populated it.
    The document is always A4 portrait, single page for typical
    monthly-subscription size.

    ``locale`` controls the language of all labels (Faktura/Rechnung,
    Dodavatel/Lieferant, etc.). Defaults to the tenant's
    ``settings["default_locale"]`` and finally to ``"cs"``. IČO, DIČ,
    DUZP and the document framing remain Czech regardless of locale —
    they are Czech regulatory artifacts, not generic words.
    """
    if locale is None:
        locale = (tenant.settings or {}).get("default_locale") or "cs"
    L = _labels_for(locale)
    regular, bold = _register_fonts()
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        title=f"Faktura {invoice.number or invoice.stripe_invoice_id or invoice.id}",
        author=settings.platform_operator_name or "Assoluto",
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
    )
    styles = getSampleStyleSheet()
    base = ParagraphStyle(
        "base", parent=styles["Normal"], fontName=regular, fontSize=10, leading=13
    )
    h1 = ParagraphStyle("h1", parent=base, fontName=bold, fontSize=16, leading=20)
    h2 = ParagraphStyle("h2", parent=base, fontName=bold, fontSize=11, leading=15)
    small = ParagraphStyle(
        "small", parent=base, fontSize=8, leading=10, textColor=colors.HexColor("#64748b")
    )

    supplier_is_vat = bool((settings.platform_operator_dic or "").strip())

    story: list = []

    # ---------- Header ----------
    doc_label = L["doc_label_vat"] if supplier_is_vat else L["doc_label_nonvat"]
    invoice_number = invoice.number or invoice.stripe_invoice_id or str(invoice.id)[:8]
    story.append(Paragraph(f"<b>{doc_label}</b>", h1))
    story.append(Paragraph(f"{L['doc_number']}: <b>{invoice_number}</b>", base))

    # Dates. DUZP for SaaS = paid_at (or issue_at if not paid yet).
    issue_date = (invoice.paid_at or invoice.created_at or datetime.now(UTC)).date()
    duzp = invoice.paid_at.date() if invoice.paid_at else issue_date
    story.append(Paragraph(f"{L['issue_date']}: {issue_date.isoformat()}", base))
    story.append(Paragraph(f"{L['duzp']}: {duzp.isoformat()}", base))
    story.append(Spacer(1, 6))

    # ---------- Parties ----------
    supplier_block = [
        Paragraph(f"<b>{L['supplier']}</b>", h2),
        Paragraph(settings.platform_operator_name or L["not_configured"], base),
        Paragraph(
            _address_with_country(
                settings.platform_operator_address or L["address_not_configured"],
                L["country"],
            ),
            base,
        ),
        Paragraph(f"IČO: {settings.platform_operator_ico or '—'}", base),
    ]
    if supplier_is_vat:
        supplier_block.append(Paragraph(f"DIČ: {settings.platform_operator_dic}", base))
    else:
        supplier_block.append(Paragraph(f"<i>{L['non_vat_payer']}</i>", small))

    # Customer side — tenant row is our direct record; we also accept
    # billing overrides in tenant.settings JSON blob.
    t_settings = tenant.settings or {}
    cust_name = t_settings.get("billing_name") or tenant.name
    cust_address = t_settings.get("billing_address") or ""
    cust_ico = t_settings.get("billing_ico") or ""
    cust_dic = t_settings.get("billing_dic") or ""

    customer_block = [
        Paragraph(f"<b>{L['customer']}</b>", h2),
        Paragraph(cust_name, base),
    ]
    if cust_address:
        customer_block.append(Paragraph(cust_address.replace("\n", "<br/>"), base))
    customer_block.append(Paragraph(f"IČO: {cust_ico or '—'}", base))
    if cust_dic:
        customer_block.append(Paragraph(f"DIČ: {cust_dic}", base))

    parties = Table(
        [[supplier_block, customer_block]],
        colWidths=[90 * mm, 90 * mm],
        style=TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        ),
    )
    story.append(parties)
    story.append(Spacer(1, 10))

    # ---------- Line items ----------
    amount_total_cents = int(invoice.amount_cents or 0)
    currency = (invoice.currency or "CZK").upper()

    if supplier_is_vat and currency == "CZK":
        # Back out the base from the gross so the maths add up to the
        # Stripe-charged amount. Stripe stores gross for us.
        gross = Decimal(amount_total_cents) / Decimal(100)
        base_amount = (gross / (Decimal(1) + CZ_VAT_STANDARD)).quantize(Decimal("0.01"))
        vat_amount = (gross - base_amount).quantize(Decimal("0.01"))
    else:
        gross = Decimal(amount_total_cents) / Decimal(100)
        base_amount = gross
        vat_amount = Decimal(0)

    period_label = ""
    if invoice.paid_at:
        period_label = f" ({invoice.paid_at.date().isoformat()})"

    item_rows: list[list[str]] = [
        [L["col_description"], L["col_base"], L["col_rate"], L["col_vat"], L["col_total"]],
        [
            f"{L['subscription_line']}{period_label}",
            f"{_dot_amount(base_amount)} {currency}",
            f"{int(CZ_VAT_STANDARD * 100)}%" if supplier_is_vat else "—",
            f"{_dot_amount(vat_amount)} {currency}" if supplier_is_vat else "—",
            f"{_dot_amount(gross)} {currency}",
        ],
    ]
    items_table = Table(
        item_rows,
        colWidths=[70 * mm, 30 * mm, 18 * mm, 28 * mm, 34 * mm],
        style=TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, 0), bold),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#334155")),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("BOX", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e2e8f0")),
            ]
        ),
    )
    story.append(items_table)
    story.append(Spacer(1, 10))

    # ---------- Summary ----------
    summary_rows = []
    if supplier_is_vat:
        summary_rows.append([L["vat_base"], f"{_dot_amount(base_amount)} {currency}"])
        summary_rows.append(
            [
                f"{L['vat_label']} {int(CZ_VAT_STANDARD * 100)}%",
                f"{_dot_amount(vat_amount)} {currency}",
            ]
        )
    summary_rows.append([f"<b>{L['total_due']}</b>", f"<b>{_dot_amount(gross)} {currency}</b>"])
    summary_data = [
        [Paragraph(label, base), Paragraph(value, base)] for label, value in summary_rows
    ]

    story.append(
        Table(
            summary_data,
            colWidths=[60 * mm, 40 * mm],
            hAlign="RIGHT",
            style=TableStyle(
                [
                    ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("LINEABOVE", (0, -1), (-1, -1), 0.5, colors.HexColor("#94a3b8")),
                ]
            ),
        )
    )
    story.append(Spacer(1, 14))

    # ---------- Payment / legal note ----------
    paid_line = (
        L["paid_via_stripe"].format(date=invoice.paid_at.date().isoformat())
        if invoice.paid_at
        else L["not_yet_paid"]
    )
    story.append(Paragraph(paid_line, small))
    story.append(Spacer(1, 4))
    story.append(Paragraph(L["electronic_notice"], small))

    doc.build(story)
    return buf.getvalue()


def _example_invoice_for_preview(*, tenant, settings):  # pragma: no cover - dev helper
    """Render a dummy invoice for manual layout inspection."""
    from datetime import datetime as _dt
    from types import SimpleNamespace

    now = _dt.now(UTC)
    fake = SimpleNamespace(
        id="preview",
        stripe_invoice_id="in_PREVIEW",
        number="2026-000001",
        amount_cents=59_900,
        currency="CZK",
        status="paid",
        paid_at=now,
        created_at=now,
    )
    return render_invoice_pdf(invoice=fake, tenant=tenant, settings=settings)  # type: ignore[arg-type]


def _safe_filename_for(invoice: Invoice) -> str:
    """Stable filename for downloads; strips anything that could break
    a Content-Disposition header."""
    number = invoice.number or invoice.stripe_invoice_id or str(invoice.id)[:8]
    cleaned = "".join(ch for ch in number if ch.isalnum() or ch in "-_")
    date_tag = (invoice.paid_at or invoice.created_at or date.today()).strftime("%Y%m%d")
    return f"assoluto-faktura-{cleaned}-{date_tag}.pdf"
