"""Fictional sample drawings for the demo tenant, rendered at seed time.

Two or three small A4 PDFs that *look* like a job shop's drawings — frame,
title block, a part outline with holes and dimensions — so the order
detail page shows real thumbnails and the download works. Every sheet
carries a large "UKÁZKA / SAMPLE" watermark and "Fiktivní výkres" in the
title block: nobody can mistake it for a real customer's drawing.

Pure function of its input (``invariant=1``), no I/O: the seed uploads
the bytes to S3 itself.
"""

# Czech drawing notes use the multiplication sign and en dash on purpose.
# ruff: noqa: RUF001, RUF002

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Literal

WATERMARK = "UKÁZKA / SAMPLE"


@dataclass(frozen=True)
class SampleDrawing:
    filename: str
    number: str
    title: str
    revision: str
    material: str
    client: str
    scale: str
    shape: Literal["bracket", "cover", "cabinet"]
    notes: tuple[str, ...] = ()


def render_drawing_pdf(drawing: SampleDrawing) -> bytes:
    """Return the PDF bytes of one fictional sample drawing (A4 landscape)."""
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.pdfgen import canvas

    from app.services.pdf_service import _register_fonts

    regular, bold = _register_fonts()
    width, height = landscape(A4)
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height), invariant=1)
    c.setTitle(f"{drawing.number} – {drawing.title} ({WATERMARK})")
    c.setAuthor("Assoluto – fiktivní ukázková data")
    c.setSubject("Fiktivní výkres pro veřejné demo")

    margin = 18
    c.setLineWidth(1.4)
    c.rect(margin, margin, width - 2 * margin, height - 2 * margin)

    _title_block(c, drawing, width, margin, regular, bold)
    {"bracket": _bracket, "cover": _cover, "cabinet": _cabinet}[drawing.shape](c, regular)
    _notes(c, drawing, margin, regular, bold)
    _watermark(c, width, height, bold)

    c.showPage()
    c.save()
    return buf.getvalue()


# ----------------------------------------------------------------- pieces


def _title_block(c, d: SampleDrawing, width: float, margin: float, regular, bold) -> None:
    bw, bh = 300, 110
    x0, y0 = width - margin - bw, margin
    c.setLineWidth(1)
    c.rect(x0, y0, bw, bh)
    rows = [
        ("Název", d.title),
        ("Číslo výkresu", f"{d.number}   rev. {d.revision}"),
        ("Materiál", d.material),
        ("Zákazník", d.client),
        ("Měřítko", f"{d.scale}      Fiktivní výkres – pouze pro demo"),
    ]
    row_h = bh / len(rows)
    for i, (label, value) in enumerate(rows):
        y = y0 + bh - (i + 1) * row_h
        if i:
            c.line(x0, y + row_h, x0 + bw, y + row_h)
        c.setFont(regular, 6.5)
        c.drawString(x0 + 4, y + row_h - 8, label)
        c.setFont(bold if i < 2 else regular, 9)
        c.drawString(x0 + 70, y + 6, value)
    c.line(x0 + 66, y0, x0 + 66, y0 + bh)


def _dim_h(c, x1: float, x2: float, y: float, text: str, regular) -> None:
    """Horizontal dimension line with arrows and the value above it."""
    c.setLineWidth(0.4)
    c.line(x1, y, x2, y)
    for x, s in ((x1, 1), (x2, -1)):
        c.line(x, y, x + 5 * s, y + 2)
        c.line(x, y, x + 5 * s, y - 2)
        c.line(x, y - 6, x, y + 6)
    c.setFont(regular, 8)
    c.drawCentredString((x1 + x2) / 2, y + 3, text)


def _dim_v(c, x: float, y1: float, y2: float, text: str, regular) -> None:
    """Vertical dimension line with arrows and the value beside it."""
    c.setLineWidth(0.4)
    c.line(x, y1, x, y2)
    for y, s in ((y1, 1), (y2, -1)):
        c.line(x, y, x + 2, y + 5 * s)
        c.line(x, y, x - 2, y + 5 * s)
        c.line(x - 6, y, x + 6, y)
    c.saveState()
    c.translate(x - 3, (y1 + y2) / 2)
    c.rotate(90)
    c.setFont(regular, 8)
    c.drawCentredString(0, 0, text)
    c.restoreState()


def _centre_mark(c, x: float, y: float, r: float) -> None:
    c.setLineWidth(0.25)
    c.setDash(6, 2)
    c.line(x - r - 4, y, x + r + 4, y)
    c.line(x, y - r - 4, x, y + r + 4)
    c.setDash()


def _bracket(c, regular) -> None:
    """Motor bracket, front view (L-plate with four holes) + side view."""
    k = 2.6  # pt per mm (≈ 1:1 on paper is too big for the sheet)
    ox, oy = 90, 230
    w, h, leg = 120 * k, 80 * k, 40 * k
    c.setLineWidth(1.2)
    p = c.beginPath()
    p.moveTo(ox, oy)
    p.lineTo(ox + w, oy)
    p.lineTo(ox + w, oy + leg)
    p.lineTo(ox + leg + 10 * k, oy + leg)
    p.lineTo(ox + leg, oy + h)
    p.lineTo(ox, oy + h)
    p.close()
    c.drawPath(p, stroke=1, fill=0)
    for hx, hy in ((15, 12), (105, 12), (15, 66), (75, 28)):
        cx, cy = ox + hx * k, oy + hy * k
        c.setLineWidth(0.9)
        c.circle(cx, cy, 4.5 * k)
        _centre_mark(c, cx, cy, 4.5 * k)
    _dim_h(c, ox, ox + w, oy - 22, "120", regular)
    _dim_v(c, ox - 22, oy, oy + h, "80", regular)
    c.setFont(regular, 8)
    c.drawString(ox + 4 * k, oy + h + 6, "4× ø9 skrz")
    # side view: bent flange, 3 mm sheet
    sx = ox + w + 60
    c.setLineWidth(1.2)
    c.rect(sx, oy, 3 * k, h)
    c.rect(sx, oy, 30 * k, 3 * k)
    _dim_h(c, sx, sx + 30 * k, oy - 22, "30", regular)
    c.drawString(sx + 10, oy + h + 6, "t = 3   R1,5 ohyb 90°")


def _cover(c, regular) -> None:
    """Gearbox cover: rounded plate, ventilation slots, fixing holes."""
    k = 1.25
    ox, oy = 80, 200
    w, h = 300 * k, 180 * k
    c.setLineWidth(1.2)
    c.roundRect(ox, oy, w, h, 12 * k)
    for i in range(6):
        c.setLineWidth(0.9)
        c.roundRect(ox + (60 + i * 32) * k, oy + 70 * k, 6 * k, 40 * k, 3 * k)
    for hx, hy in ((15, 15), (285, 15), (15, 165), (285, 165)):
        cx, cy = ox + hx * k, oy + hy * k
        c.circle(cx, cy, 3.25 * k)
        _centre_mark(c, cx, cy, 3.25 * k)
    _dim_h(c, ox, ox + w, oy - 22, "300", regular)
    _dim_v(c, ox - 22, oy, oy + h, "180", regular)
    c.setFont(regular, 8)
    c.drawString(ox + 60 * k, oy + 120 * k, "6× drážka 40×6, rozteč 32")
    c.drawString(ox + 20 * k, oy + 25 * k, "4× ø6,5")
    c.drawString(ox, oy + h + 10, "R12 všechny rohy · lem 15 mm ohnout 90° dovnitř")


def _cabinet(c, regular) -> None:
    """Electrical cabinet 600×400, front view (door, hinges, lock) + depth."""
    k = 0.55  # 1:5 on the sheet
    ox, oy = 90, 160
    w, h = 400 * k, 600 * k
    c.setLineWidth(1.2)
    c.rect(ox, oy, w, h)
    c.setLineWidth(0.8)
    c.rect(ox + 15 * k, oy + 15 * k, w - 30 * k, h - 30 * k)
    for hy in (90, 510):
        c.rect(ox + 6 * k, oy + hy * k, 8 * k, 40 * k, fill=1)
    c.circle(ox + w - 35 * k, oy + h / 2, 9 * k)
    _centre_mark(c, ox + w - 35 * k, oy + h / 2, 9 * k)
    _dim_h(c, ox, ox + w, oy - 22, "400", regular)
    _dim_v(c, ox - 22, oy, oy + h, "600", regular)
    sx = ox + w + 80
    c.setLineWidth(1.2)
    c.rect(sx, oy, 250 * k, h)
    _dim_h(c, sx, sx + 250 * k, oy - 22, "250", regular)
    c.setFont(regular, 8)
    c.drawString(sx, oy + h + 8, "Bok · DC01 1,5 mm")
    c.drawString(ox, oy + h + 8, "Čelo · zámek ø18 · 2 závěsy")


def _notes(c, d: SampleDrawing, margin: float, regular, bold) -> None:
    lines = (
        *d.notes,
        "Nekótované rozměry ±0,5 mm (ISO 2768-m)",
        "Odjehlit všechny hrany",
    )
    x, y = margin + 12, margin + 12 + 11 * (len(lines) - 1)
    c.setFont(bold, 8)
    c.drawString(x, y + 13, "Poznámky:")
    c.setFont(regular, 8)
    for line in lines:
        c.drawString(x, y, f"– {line}")
        y -= 11


def _watermark(c, width: float, height: float, bold) -> None:
    c.saveState()
    c.setFillColorRGB(0.82, 0.12, 0.12)
    c.setFillAlpha(0.16)
    c.translate(width / 2, height / 2 + 20)
    c.rotate(24)
    c.setFont(bold, 64)
    c.drawCentredString(0, 0, WATERMARK)
    c.setFont(bold, 18)
    c.drawCentredString(0, -36, "Fiktivní výkres · Fictional drawing · Assoluto demo")
    c.restoreState()
