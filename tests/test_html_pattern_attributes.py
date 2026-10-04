"""HTML ``pattern`` attributes are compiled with the RegExp ``v`` flag in
current browsers. Under ``v`` an unescaped ``-`` at the edge of a character
class is a syntax error, and the browser then ignores the pattern entirely —
the signup slug field silently lost its client-side check (found by the
2026-10 browser E2E run as a console error)."""

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"


def test_pattern_attributes_are_valid_under_the_v_flag():
    bad = []
    for path in TEMPLATES.rglob("*.html"):
        for pattern in re.findall(r'pattern="([^"]*)"', path.read_text()):
            for cls in re.findall(r"\[([^\]]*)\]", pattern):
                body = cls.replace("\\-", "")
                if body.startswith("-") or body.endswith("-"):
                    bad.append(f"{path.relative_to(TEMPLATES)}: {pattern}")
    assert not bad, bad
