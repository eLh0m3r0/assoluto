"""CS message catalog health checks.

Catches the failure modes that ate the homepage twice in two days:

1. Active msgids with EMPTY msgstr → ``compile`` keeps them but
   gettext falls back to the EN msgid → user sees English on a CS-
   default page.
2. Active msgids marked ``#, fuzzy`` → ``compile`` DROPS them by
   default → same EN fallback (or, if the fuzzy assignment happens to
   sneak through, a wrong CS sentence).

Both surface naturally in walking the live site, but by then the user
has shipped the regression. Run on every CI pass instead.

Run only against ``app/locale/cs/LC_MESSAGES/messages.po`` — the EN
catalog is intentionally an empty-msgstr identity catalog (gettext
falls back to msgid which IS the English text) so this check would
generate noise there.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CS_PO = REPO_ROOT / "app" / "locale" / "cs" / "LC_MESSAGES" / "messages.po"


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


def test_no_empty_msgstr_in_cs_catalog() -> None:
    """Every active CS msgid must have a non-empty translation. Empty
    msgstr → gettext falls back to the EN msgid on the CS-default site.
    """
    text = CS_PO.read_text(encoding="utf-8")
    entries = _parse_active_entries(text)
    empty = [e for e in entries if e["msgstr"] == ""]
    assert not empty, (
        f"{len(empty)} active CS entries have empty msgstr. Examples:\n  "
        + "\n  ".join(repr(e["msgid"][:80]) for e in empty[:10])
        + (f"\n  … (+{len(empty) - 10} more)" if len(empty) > 10 else "")
        + "\n\nFix: add CS translations in app/locale/cs/LC_MESSAGES/messages.po"
        " then ``uv run pybabel compile -d app/locale``. If the missing"
        " translation has a CS twin in the obsolete (#~) section of the"
        " same file, the rescue scripts at /tmp/resurrect_obsolete.py and"
        " /tmp/resurrect_v2.py recover those automatically (see commit"
        " e3eaade for context)."
    )


def test_no_fuzzy_entries_in_cs_catalog() -> None:
    """No active CS msgid should carry the ``#, fuzzy`` flag. Babel
    compile drops fuzzy entries by default → user sees EN fallback,
    same effective outcome as test_no_empty_msgstr_in_cs_catalog.

    Removing the flag is the right fix when the existing translation
    is correct; rewriting the msgstr is right when the fuzzy match
    attached an unrelated translation. Either way, the flag must go.
    """
    text = CS_PO.read_text(encoding="utf-8")
    entries = _parse_active_entries(text)
    fuzzy = [e for e in entries if e["fuzzy"]]
    assert not fuzzy, (
        f"{len(fuzzy)} active CS entries are fuzzy-flagged. Compile drops"
        f" them → the EN msgid shows on the CS-default site. Examples:\n  "
        + "\n  ".join(f"{e['msgid'][:60]!r} → {e['msgstr'][:60]!r}" for e in fuzzy[:10])
        + (f"\n  … (+{len(fuzzy) - 10} more)" if len(fuzzy) > 10 else "")
        + "\n\nFix: edit app/locale/cs/LC_MESSAGES/messages.po, drop the"
        " ``#, fuzzy`` line from each affected entry (after confirming"
        " the msgstr is the right translation), then ``uv run pybabel"
        " compile -d app/locale``."
    )


# A small smoke test: spot-check that a handful of the previously-
# regressed strings have the right translation now. If a future Babel
# update silently swaps these out we'll see it before it ships.
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
