"""PDF previews need poppler in the image — they silently never worked in prod
(audit 2026-10-03 BE-04: pdf2image was not a dependency, poppler not installed)."""

import shutil
from io import BytesIO

import pytest
from reportlab.pdfgen import canvas

from app.tasks.thumbnail_tasks import _render_thumbnail


def test_pdf2image_is_installed():
    import pdf2image  # noqa: F401 — the import itself is the assertion


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="poppler-utils not installed")
def test_pdf_first_page_renders_to_jpeg():
    buf = BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(100, 750, "Drawing M8 rev C")
    c.save()
    out = _render_thumbnail(buf.getvalue(), "application/pdf")
    assert out is not None and out[:3] == b"\xff\xd8\xff"


def test_garbage_pdf_does_not_raise():
    assert _render_thumbnail(b"%PDF-1.4 not really", "application/pdf") is None
