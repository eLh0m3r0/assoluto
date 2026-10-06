"""Fictional sample drawings for the demo tenant, rendered at seed time.

Small A4 PDFs that *look* like a job shop's drawings — frame, title
block, a part outline with holes and dimensions — plus a couple of PNG
"3D previews", so the order detail page shows real thumbnails and the
download works. Every sheet and every preview carries a large
"UKÁZKA / SAMPLE" watermark and "Fiktivní výkres" in the title block:
nobody can mistake it for a real customer's drawing.

Pure functions of their input (``invariant=1``, no timestamps), no I/O:
the seed uploads the bytes to S3 itself.
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
    shape: Literal["bracket", "cover", "cabinet", "flange", "plate", "frame"]
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SamplePreview:
    """A shaded isometric "3D preview" PNG of a part (what a client exports
    from CAD next to the drawing)."""

    filename: str
    number: str
    title: str
    client: str
    shape: Literal["bracket", "cover"]


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
    shapes = {
        "bracket": _bracket,
        "cover": _cover,
        "cabinet": _cabinet,
        "flange": _flange,
        "plate": _plate,
        "frame": _frame,
    }
    shapes[drawing.shape](c, regular)
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


def _flange(c, regular) -> None:
    """Turned flange: front view (bolt circle, bore) + half section."""
    k = 1.6
    cx, cy = 210, 330
    r_out, r_bc, r_bore, r_hole = 60 * k, 45 * k, 15 * k, 4.5 * k
    c.setLineWidth(1.2)
    c.circle(cx, cy, r_out)
    c.circle(cx, cy, r_bore)
    c.setLineWidth(0.25)
    c.setDash(6, 2)
    c.circle(cx, cy, r_bc)
    c.setDash()
    _centre_mark(c, cx, cy, r_out)
    import math

    c.setLineWidth(0.9)
    for i in range(6):
        a = math.radians(30 + i * 60)
        hx, hy = cx + r_bc * math.cos(a), cy + r_bc * math.sin(a)
        c.circle(hx, hy, r_hole)
    _dim_h(c, cx - r_out, cx + r_out, cy - r_out - 24, "ø120", regular)
    c.setFont(regular, 8)
    c.drawString(cx + r_out - 10, cy + r_out + 4, "6× ø9 na ø90")
    c.drawString(cx - 18, cy - 8, "ø30 H7")
    # section: hub + flange
    sx, sy = 400, cy - r_out
    c.setLineWidth(1.2)
    c.rect(sx, sy, 12 * k, 2 * r_out)
    c.rect(sx + 12 * k, cy - 25 * k, 18 * k, 50 * k)
    c.setLineWidth(0.3)
    for i in range(0, int(2 * r_out), 8):
        c.line(sx, sy + i, sx + 12 * k, sy + min(i + 12 * k, 2 * r_out))
    _dim_h(c, sx, sx + 30 * k, sy - 24, "30", regular)
    c.drawString(sx, sy + 2 * r_out + 6, "Řez A–A · Ra 1,6 na ø30")


def _plate(c, regular) -> None:
    """Clamping / mounting plate: hole pattern, pocket + side view."""
    k = 0.9
    ox, oy = 80, 170
    w, h = 400 * k, 300 * k
    c.setLineWidth(1.2)
    c.rect(ox, oy, w, h)
    c.setLineWidth(0.9)
    c.roundRect(ox + 120 * k, oy + 90 * k, 160 * k, 120 * k, 10 * k)
    for hx in (40, 140, 260, 360):
        for hy in (40, 260):
            cx, cy = ox + hx * k, oy + hy * k
            c.circle(cx, cy, 5 * k)
            _centre_mark(c, cx, cy, 5 * k)
    _dim_h(c, ox, ox + w, oy - 22, "400", regular)
    _dim_v(c, ox - 22, oy, oy + h, "300", regular)
    c.setFont(regular, 8)
    c.drawString(ox + 4, oy + h + 8, "8× M10 – 20 hl. · kapsa 160×120 hl. 8, R10")
    sx = ox + w + 50
    c.setLineWidth(1.2)
    c.rect(sx, oy, 30 * k, h)
    _dim_h(c, sx, sx + 30 * k, oy - 22, "30", regular)
    c.drawString(sx - 10, oy + h + 8, "rovinnost 0,02")


def _frame(c, regular) -> None:
    """Welded frame from square tube: front view + base plates."""
    k = 0.42
    ox, oy = 90, 160
    w, h, t = 800 * k, 600 * k, 40 * k
    c.setLineWidth(1.2)
    c.rect(ox, oy, w, h)
    c.rect(ox + t, oy + t, w - 2 * t, h - 2 * t)
    c.setLineWidth(0.9)
    c.rect(ox + t, oy + h / 2 - t / 2, w - 2 * t, t)  # cross member
    for px in (ox - 20 * k, ox + w - 100 * k):
        c.rect(px, oy - 8 * k, 120 * k, 8 * k)  # base plates t = 8
    _dim_h(c, ox, ox + w, oy - 30, "800", regular)
    _dim_v(c, ox - 26, oy, oy + h, "600", regular)
    c.setFont(regular, 8)
    c.drawString(ox, oy + h + 8, "Jäkl 40×40×3 · svary a5 po obvodu · patky P8 120×120, 4× ø14")


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


# ------------------------------------------------------------ PNG preview


def render_preview_png(preview: SamplePreview) -> bytes:
    """Return the PNG bytes of a shaded isometric preview of the part."""
    import math

    from PIL import Image, ImageDraw, ImageFont

    from app.services.pdf_service import FONTS_DIR

    width, height = 1200, 800
    img = Image.new("RGB", (width, height), (246, 247, 249))
    draw = ImageDraw.Draw(img)

    def font(size: int, bold: bool = False):
        name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
        try:
            return ImageFont.truetype(str(FONTS_DIR / name), size)
        except OSError:  # pragma: no cover - fonts ship with the repo
            return ImageFont.load_default()

    cos30, sin30 = math.cos(math.radians(30)), 0.5
    scale, cx, cy = (
        (1.85, width / 2, height / 2 + 10)
        if preview.shape == "cover"
        else (
            3.6,
            width / 2 + 20,
            height / 2 + 90,
        )
    )

    def p(x: float, y: float, z: float) -> tuple[float, float]:
        return (cx + (x - y) * cos30 * scale, cy + ((x + y) * sin30 - z) * scale)

    def box(x0, x1, y0, y1, z0, z1, base=(176, 184, 196)) -> None:
        top = [p(x0, y0, z1), p(x1, y0, z1), p(x1, y1, z1), p(x0, y1, z1)]
        side_x = [p(x1, y0, z0), p(x1, y1, z0), p(x1, y1, z1), p(x1, y0, z1)]
        side_y = [p(x0, y1, z0), p(x1, y1, z0), p(x1, y1, z1), p(x0, y1, z1)]
        outline = (60, 66, 78)
        draw.polygon(side_x, fill=tuple(int(v * 0.78) for v in base), outline=outline)
        draw.polygon(side_y, fill=tuple(int(v * 0.62) for v in base), outline=outline)
        draw.polygon(top, fill=base, outline=outline)

    def hole(x: float, y: float, z: float, r: float) -> None:
        pts = [
            p(x + r * math.cos(a / 12 * math.pi), y + r * math.sin(a / 12 * math.pi), z)
            for a in range(24)
        ]
        draw.polygon(pts, fill=(70, 76, 88))

    if preview.shape == "cover":
        box(-150, 150, -90, 90, 0, 15, base=(64, 112, 170))  # RAL 5010-ish
        for i in range(6):
            x = -90 + i * 32
            draw.polygon(
                [p(x, -20, 15), p(x + 6, -20, 15), p(x + 6, 20, 15), p(x, 20, 15)],
                fill=(28, 44, 70),
            )
        for hx, hy in ((-135, -75), (135, -75), (-135, 75), (135, 75)):
            hole(hx, hy, 15, 3.5)
    else:  # bracket: base plate + bent flange, 3 mm sheet
        box(-60, 60, -40, 40, 0, 3)
        for hx, hy in ((-45, -28), (45, -28), (15, 0), (45, 28)):
            hole(hx, hy, 3, 4.5)
        box(-60, -57, -40, 40, 3, 33)

    draw.text(
        (40, 32), f"{preview.number} – {preview.title}", font=font(30, True), fill=(30, 34, 42)
    )
    draw.text((40, 74), f"3D náhled · {preview.client}", font=font(20), fill=(90, 96, 108))
    draw.text(
        (40, height - 48),
        "Fiktivní model – pouze pro demo · Assoluto",
        font=font(18),
        fill=(90, 96, 108),
    )

    # Watermark on its own layer, rotated, then blended in.
    mark = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    mdraw = ImageDraw.Draw(mark)
    big = font(96, True)
    box_w = mdraw.textlength(WATERMARK, font=big)
    mdraw.text(((width - box_w) / 2, height / 2 - 70), WATERMARK, font=big, fill=(210, 31, 31, 60))
    small = font(26, True)
    line = "Fiktivní výkres · Fictional drawing · Assoluto demo"
    mdraw.text(
        ((width - mdraw.textlength(line, font=small)) / 2, height / 2 + 50),
        line,
        font=small,
        fill=(210, 31, 31, 60),
    )
    mark = mark.rotate(20, resample=Image.Resampling.BICUBIC)
    img = Image.alpha_composite(img.convert("RGBA"), mark).convert("RGB")

    out = BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()
