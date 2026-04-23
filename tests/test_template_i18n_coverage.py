"""Lint: every user-visible string in a template is translatable.

This is the guard that turns "oh look another hardcoded Czech string"
into a CI failure instead of a post-ship user report. Runs on every
PR — no Postgres required.

The scanner is tolerant of legitimate unwrapped text: language names
like "Čeština" that label themselves, fully-dynamic ``{{ var }}``
outputs, code blocks, comments, attribute values that never reach a
human (``class``, ``data-*``, ``href``, ``src``), and the email
templates (which have their own audit track).

Any diacritic or multi-word English string that leaks past all of
that is almost certainly a missing ``{{ _(...) }}``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "app" / "templates"

# Email templates live under app/email/templates/ and aren't touched by this
# guard — they have their own i18n rollout planned (see email-audit report).
# Everything under app/templates/ should be covered.
EMAIL_PREFIX = REPO_ROOT / "app" / "email" / "templates"

# A pre-approved allow-list of strings that render unwrapped *on purpose*.
# Keep tight — every entry needs a justification in the comment above it,
# and adding a new one should be a deliberate exception, not a band-aid
# for "I don't want to translate this yet".
ALLOWED_LITERALS: set[str] = {
    # Language self-labels — conventionally kept in their own name across
    # locales. Already handled via aria-label="Čeština" and inner text
    # inside the CS/EN switcher.
    "Čeština",
    "English",
    "CS",
    "EN",
    # Legal entity IDs (column headings keep the Czech acronym for legal
    # recognisability even under the English UI).
    "IČO",
    "DIČ",
    # App-wide brand wordmark and version.
    "Assoluto",
    # Placeholder-only content — demo / sample values surfaced as
    # ``placeholder="{{ _('Jan Novák') }}"`` style patterns. These are
    # intentionally Czech-only because they're demo data illustrating
    # the format, not user-visible copy in the running product.
    "Jan Novák",
    "ACME s.r.o.",
    "name@company.com",
    # Mock client firm names in the hero screenshot — intentionally Czech
    # to signal "this is for Czech firms" regardless of UI locale.
    "Kovárna Vlček",
    "Zámečnictví Horák",
    "Novák Engineering",
    # Units / currencies that are language-neutral.
    "CZK",
    "EUR",
    "USD",
    "Kč",
    # 'SLA' and 'PDF' and 'CSV' — TLAs that don't translate.
    "SLA",
    "PDF",
    "CSV",
    "MRR",
    "SKU",
    "Esc",
    "URL",
    "ID",
    # Glyph-only or punctuation-only captions.
    "·",
    "&larr;",
    "&rarr;",
    "→",
    "←",
    "—",  # em dash
    "–",  # en dash — legitimate separator glyph, not a typo  # noqa: RUF001
    "↔",
    "⚙",
    "⬇",
    "📄",
}

# Czech diacritic characters. Any text containing one of these that's not
# already inside ``{{ _(...) }}`` is almost certainly an oversight.
CZECH_DIACRITICS = re.compile(r"[áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]")

# Jinja gettext aliases and translation blocks — content within these is
# already translatable.
GETTEXT_CALL_RE = re.compile(r"""\{\{-?\s*_\s*\(\s*['"][^'"]*['"]\s*\)\s*-?\}\}""")
TRANS_BLOCK_RE = re.compile(r"\{%\s*trans\s*%\}.*?\{%\s*endtrans\s*%\}", re.DOTALL)

# A ``{{ var }}`` / ``{{ var | filter }}`` expression with no string literals.
JINJA_VAR_RE = re.compile(r"""\{\{-?\s*[^'"{}]*?\s*-?\}\}""")

# Anything inside Jinja control blocks (``{% ... %}``) and Jinja comments
# (``{# ... #}``).
JINJA_STMT_RE = re.compile(r"\{%.*?%\}", re.DOTALL)
JINJA_COMMENT_RE = re.compile(r"\{#.*?#\}", re.DOTALL)

# HTML comments.
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# <script>, <style>, <pre>, <code> — tooling / code content is out of scope.
SKIP_TAGS_RE = re.compile(r"<(script|style|pre|code)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)

# Attributes we check for translatable copy — user-facing only.
#
# We intentionally DO check ``placeholder``, ``title``, ``aria-label``,
# ``alt`` — screen-reader text and tooltips must be translatable too.
USER_VISIBLE_ATTR_RE = re.compile(
    r'''(?:placeholder|title|aria-label|alt|aria-describedby-tooltip)="([^"]*)"''',
    re.IGNORECASE,
)


def _strip_templated(text: str) -> str:
    """Return ``text`` with every fragment that's already translatable or
    non-user-facing removed. What remains is the raw *possibly-untranslated*
    content between tags and in attribute values."""
    for pattern in (
        SKIP_TAGS_RE,
        HTML_COMMENT_RE,
        JINJA_COMMENT_RE,
        TRANS_BLOCK_RE,
        GETTEXT_CALL_RE,
        JINJA_STMT_RE,
    ):
        text = pattern.sub(" ", text)
    # JINJA_VAR_RE after the others so ``{{ _('x') }}`` is stripped by the
    # dedicated rule first.
    text = JINJA_VAR_RE.sub(" ", text)
    return text


def _text_nodes(cleaned: str) -> list[str]:
    """Yield the raw text content between HTML tags, excluding attribute
    values (those are handled separately)."""
    # Remove all tags. ``<tag …>content</tag>`` → ``content``.
    text_only = re.sub(r"<[^>]+>", " ", cleaned)
    return [chunk.strip() for chunk in re.split(r"\s{2,}|\n", text_only) if chunk.strip()]


def _attr_values(cleaned: str) -> list[str]:
    return [
        m.group(1).strip() for m in USER_VISIBLE_ATTR_RE.finditer(cleaned) if m.group(1).strip()
    ]


def _is_allowed(literal: str) -> bool:
    """A text chunk is OK to leave unwrapped if, after stripping each
    allow-listed token + punctuation + digits, nothing substantive
    remains. Handles joined fragments like '· DIČ ' where a bullet
    separator borders a Czech legal ID both of which are legitimate
    non-translation tokens.
    """
    if literal in ALLOWED_LITERALS:
        return True
    # Whitespace-only or pure punctuation / digits / HTML entity.
    if re.fullmatch(r"[\s\d\W]*", literal):
        return True
    if re.fullmatch(r"&[a-z]+;", literal):
        return True

    # Walk word-like tokens; if every Czech-diacritic-bearing token is in
    # the allow-list, the literal is fine even if there's connective
    # punctuation / separators / numbers between them.
    tokens = [t for t in re.findall(r"[^\s\d\W]+|\S", literal) if t]
    czech_tokens = [t for t in tokens if CZECH_DIACRITICS.search(t)]
    return bool(czech_tokens) and all(t in ALLOWED_LITERALS for t in czech_tokens)


def _contains_czech(literal: str) -> bool:
    return bool(CZECH_DIACRITICS.search(literal))


def _collect_templates() -> list[Path]:
    if not TEMPLATES.exists():
        return []
    return [p for p in TEMPLATES.rglob("*.html") if EMAIL_PREFIX not in p.parents]


def _scan(path: Path) -> list[str]:
    """Return a list of offending literals found in ``path``, or empty."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        return [f"<read error: {exc}>"]
    cleaned = _strip_templated(text)
    hits: list[str] = []
    for literal in _text_nodes(cleaned):
        if _is_allowed(literal):
            continue
        if _contains_czech(literal):
            hits.append(literal)
    for literal in _attr_values(cleaned):
        if _is_allowed(literal):
            continue
        if _contains_czech(literal):
            hits.append(f"(attr) {literal}")
    return hits


@pytest.mark.parametrize(
    "template_path",
    _collect_templates(),
    ids=lambda p: str(p.relative_to(REPO_ROOT)),
)
def test_template_has_no_untranslated_czech(template_path: Path) -> None:
    """Fail if a template contains Czech diacritic text outside ``{{ _() }}``.

    The guard only flags Czech-specific letters (š, č, ř, ž, á, …). Pure
    ASCII English strings slip through — they're caught by the English
    msgid-style wrap convention, not this guard, and they at least do not
    produce bilingual mush when the user switches the locale.

    If a string is legitimately not translatable (language self-labels,
    legal IDs, demo data), add it to ``ALLOWED_LITERALS`` above with a
    comment explaining why.
    """
    hits = _scan(template_path)
    assert not hits, (
        f"{template_path.relative_to(REPO_ROOT)} contains {len(hits)} raw "
        f"Czech string(s) outside a gettext wrapper. Wrap them in "
        f"``{{{{ _('English msgid') }}}}`` and add a CS translation in "
        f"``app/locale/cs/LC_MESSAGES/messages.po``, then compile with "
        f"``pybabel compile -d app/locale``. Offending literals:\n  "
        + "\n  ".join(repr(h) for h in hits[:20])
        + ("\n  … (+ more)" if len(hits) > 20 else "")
    )
