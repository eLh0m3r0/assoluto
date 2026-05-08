"""Translated-catalog health checks (CS + DE).

Catches the failure modes that ate the homepage twice in two days:

1. Active msgids with EMPTY msgstr → ``compile`` keeps them but
   gettext falls back to the EN msgid → a CS-default visitor sees
   English (and a DE-default visitor sees English instead of German).
2. Active msgids marked ``#, fuzzy`` → ``compile`` DROPS them by
   default → same EN fallback (or, if the fuzzy assignment happens to
   sneak through, a wrong CS / DE sentence).

Both surface naturally in walking the live site, but by then the user
has shipped the regression. Run on every CI pass instead.

The EN catalog is intentionally an empty-msgstr *identity* catalog —
gettext falls back to the msgid which IS the English text — so we
exclude it from these checks.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CS_PO = REPO_ROOT / "app" / "locale" / "cs" / "LC_MESSAGES" / "messages.po"
DE_PO = REPO_ROOT / "app" / "locale" / "de" / "LC_MESSAGES" / "messages.po"

# Locales we ship a real (non-identity) translation for. Keep this in
# sync with ``SUPPORTED_LOCALES`` minus the identity-catalog locales.
TRANSLATED_LOCALES = ("cs", "de")


def _po_path(locale: str) -> Path:
    return REPO_ROOT / "app" / "locale" / locale / "LC_MESSAGES" / "messages.po"


def _parse_active_entries(po_text: str) -> list[dict]:
    """Walk the PO file and return active (non-#~) entries with their
    msgid, msgstr (multi-line concatenated), and whether they carry the
    ``#, fuzzy`` flag. Skip the metadata header (msgid '').
    """
    lines = po_text.split("\n")
    entries: list[dict] = []

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.lstrip()

        # Skip obsolete (#~) blocks entirely.
        if stripped.startswith("#~"):
            i += 1
            continue

        if not re.match(r'\s*msgid\s+"', line):
            i += 1
            continue

        # Walk back for any leading comments (#, # this is a flag like
        # `#, fuzzy`, or `#:` location lines).
        start = i
        while start > 0:
            prev = lines[start - 1].lstrip()
            if prev.startswith("#") and not prev.startswith("#~"):
                start -= 1
            else:
                break

        # Detect fuzzy flag in [start, i).
        is_fuzzy = any(lines[j].strip() == "#, fuzzy" for j in range(start, i))

        # Read msgid (multi-line).
        msgid_parts: list[str] = []
        m = re.match(r'\s*msgid\s+"(.*)"\s*$', lines[i])
        if m:
            msgid_parts.append(m.group(1))
        i += 1
        while i < n:
            ln = lines[i]
            cm = re.match(r'\s*"(.*)"\s*$', ln)
            if cm and not ln.lstrip().startswith("msgstr"):
                msgid_parts.append(cm.group(1))
                i += 1
            else:
                break

        # Read msgstr (multi-line).
        if i >= n or not re.match(r'\s*msgstr\s+"', lines[i]):
            continue
        ms_match = re.match(r'\s*msgstr\s+"(.*)"\s*$', lines[i])
        msgstr_parts: list[str] = [ms_match.group(1)] if ms_match else []
        i += 1
        while i < n:
            ln = lines[i]
            cm = re.match(r'\s*"(.*)"\s*$', ln)
            if cm:
                msgstr_parts.append(cm.group(1))
                i += 1
            else:
                break

        msgid = "".join(msgid_parts)
        msgstr = "".join(msgstr_parts)

        # Skip the metadata header (msgid '' carries the .po header).
        if msgid == "":
            continue

        entries.append({"msgid": msgid, "msgstr": msgstr, "fuzzy": is_fuzzy})

    return entries


# ---------------------------------------------------------------------------


@pytest.mark.parametrize("locale", TRANSLATED_LOCALES)
def test_no_empty_msgstr(locale: str) -> None:
    """Every active msgid in a *translated* catalog must have a
    non-empty msgstr. Empty msgstr → gettext falls back to the EN
    msgid, which silently degrades the page to English."""
    text = _po_path(locale).read_text(encoding="utf-8")
    entries = _parse_active_entries(text)
    empty = [e for e in entries if e["msgstr"] == ""]
    assert not empty, (
        f"{len(empty)} active {locale.upper()} entries have empty msgstr."
        " Examples:\n  "
        + "\n  ".join(repr(e["msgid"][:80]) for e in empty[:10])
        + (f"\n  … (+{len(empty) - 10} more)" if len(empty) > 10 else "")
        + f"\n\nFix: add {locale.upper()} translations in"
        f" app/locale/{locale}/LC_MESSAGES/messages.po then"
        " ``.venv/bin/pybabel compile -d app/locale``."
    )


@pytest.mark.parametrize("locale", TRANSLATED_LOCALES)
def test_no_fuzzy_entries(locale: str) -> None:
    """No active msgid in a translated catalog should carry the
    ``#, fuzzy`` flag. Babel ``compile`` drops fuzzy entries by
    default → the EN msgid shows instead of the intended translation.

    Removing the flag is the right fix when the existing translation
    is correct; rewriting the msgstr is right when the fuzzy match
    attached an unrelated translation. Either way, the flag must go.
    """
    text = _po_path(locale).read_text(encoding="utf-8")
    entries = _parse_active_entries(text)
    fuzzy = [e for e in entries if e["fuzzy"]]
    assert not fuzzy, (
        f"{len(fuzzy)} active {locale.upper()} entries are fuzzy-flagged."
        " Compile drops them → the EN msgid shows. Examples:\n  "
        + "\n  ".join(f"{e['msgid'][:60]!r} → {e['msgstr'][:60]!r}" for e in fuzzy[:10])
        + (f"\n  … (+{len(fuzzy) - 10} more)" if len(fuzzy) > 10 else "")
        + f"\n\nFix: edit app/locale/{locale}/LC_MESSAGES/messages.po, drop"
        " the ``#, fuzzy`` line from each affected entry, then"
        " ``.venv/bin/pybabel compile -d app/locale``."
    )


# A small smoke test: spot-check that a handful of the previously-
# regressed strings have the right translation now. If a future Babel
# update silently swaps these out we'll see it before it ships.
# ---------------------------------------------------------------------------
# EN identity-catalog hygiene
# ---------------------------------------------------------------------------
#
# The EN catalog is intentionally an *identity catalog* — empty msgstrs
# everywhere, gettext falls back to the msgid which IS the English text.
# But ``pybabel update`` can silently mark dozens of msgids as ``#, fuzzy``
# with completely unrelated msgstrs (e.g. ``msgid "Message sent" msgstr
# "Manage tenants"``). gettext skips fuzzies, so the visible page stays
# correct — but the catalog sits on a tripwire: anyone clearing flags
# casually, or a future tooling change that promotes fuzzies, would ship
# the nonsense to prospects. F-BIZ-011 found 175 such entries dormant in
# the EN catalog. These two tests are the regression guard.

EN_PO = REPO_ROOT / "app" / "locale" / "en" / "LC_MESSAGES" / "messages.po"


def test_en_catalog_has_no_fuzzy_entries() -> None:
    """The EN identity catalog must not carry ``#, fuzzy`` flags. Even
    though Babel ``compile`` drops them at runtime, leaving them in the
    .po source means the next operator who runs ``pybabel update`` and
    reviews the diff sees nonsense msgstrs that have to be cleaned up
    manually before the next translator round-trip. Easier to keep the
    catalog clean."""
    text = EN_PO.read_text(encoding="utf-8")
    entries = _parse_active_entries(text)
    fuzzy = [e for e in entries if e["fuzzy"]]
    assert not fuzzy, (
        f"{len(fuzzy)} fuzzy entries in the EN identity catalog. "
        "Examples:\n  "
        + "\n  ".join(f"{e['msgid'][:60]!r} → {e['msgstr'][:60]!r}" for e in fuzzy[:5])
        + (f"\n  … (+{len(fuzzy) - 5} more)" if len(fuzzy) > 5 else "")
        + "\n\nFix: drop the ``#, fuzzy`` line from each affected entry, "
        "and either (a) clear the msgstr (the EN identity fallback then "
        "kicks in correctly) or (b) supply a real EN translation. Then "
        "``.venv/bin/pybabel compile -d app/locale``."
    )


def test_en_catalog_msgstrs_are_empty_or_match_msgid() -> None:
    """In an identity catalog, every active msgstr is either empty
    (gettext falls back to the msgid) or exactly equal to the msgid
    (a literal rewrite of the source string for translator tooling).
    Anything else means a real translation has crept in — usually as a
    fuzzy-resolution accident — and the page will show that
    translation instead of the source string the developer typed."""
    text = EN_PO.read_text(encoding="utf-8")
    entries = _parse_active_entries(text)
    drift = [e for e in entries if e["msgstr"] and e["msgstr"] != e["msgid"]]
    assert not drift, (
        f"{len(drift)} EN msgstrs differ from their msgid. "
        "Examples:\n  "
        + "\n  ".join(f"{e['msgid'][:60]!r} → {e['msgstr'][:60]!r}" for e in drift[:5])
        + (f"\n  … (+{len(drift) - 5} more)" if len(drift) > 5 else "")
        + '\n\nFix: clear the msgstr (set it to ``""``) so the identity '
        "fallback takes over, or update the msgid in the source template "
        "to match the intended copy."
    )


@pytest.mark.parametrize(
    "msgid,must_contain",
    [
        ("Stop picking up the phone.", "Přestaňte zvedat telefon"),
        ("Need more?", "Potřebujete víc"),
        ("Your server, your data", "server"),
        ("Why Assoluto", "Assoluto"),  # CS translation should mention Assoluto
        ("How it works", "Jak to funguje"),
    ],
)
def test_canonical_cs_translation_present(msgid: str, must_contain: str) -> None:
    """Spot-check known previously-regressed homepage strings have a CS
    translation containing the expected stem. Catches a future fuzzy
    swap before it ships.
    """
    text = CS_PO.read_text(encoding="utf-8")
    entries = _parse_active_entries(text)
    matches = [e for e in entries if e["msgid"] == msgid]
    assert matches, f"msgid {msgid!r} not found in CS catalog at all"
    # An entry might appear twice across template + JSON-LD; either
    # one having the right translation is enough for the user.
    have_good = any(must_contain.lower() in e["msgstr"].lower() for e in matches)
    assert have_good, (
        f"msgid {msgid!r} present but CS msgstr does not contain"
        f" expected stem {must_contain!r}. Current msgstr(s):\n  "
        + "\n  ".join(repr(e["msgstr"]) for e in matches)
    )
