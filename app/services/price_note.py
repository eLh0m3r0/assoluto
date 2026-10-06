"""Tenant "price note" — a free-text line printed at the bottom of order PDFs.

Whether prices are net or gross, VAT or no VAT, payment terms: the
portal cannot know (a supplier may or may not be a VAT payer), so it
never prints a claim of its own on a commercial document (demo review
P2-3). The tenant admin writes the sentence that is true for them on
``/app/admin/tenant-settings`` — e.g. "Prices are exclusive of VAT." or
"We are not a VAT payer." — and every order PDF carries it.

Stored in ``tenants.settings["price_note"]`` (no column, no migration).
Empty / absent = nothing is printed, which is the default.
"""

from __future__ import annotations

from typing import Any

#: Key inside ``tenants.settings``.
SETTINGS_KEY = "price_note"

#: A note, not a contract: long enough for two or three sentences.
MAX_LENGTH = 500

#: Lines kept; anything beyond is joined onto the last one.
MAX_LINES = 4


def normalize_price_note(raw: Any) -> str:
    """Trimmed note with ``\\n`` line ends and at most :data:`MAX_LINES` lines.

    Blank lines are dropped. Does not truncate — callers reject a note
    over :data:`MAX_LENGTH` so the admin sees the error.
    """
    if not isinstance(raw, str):
        return ""
    lines = [line.strip() for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    lines = [line for line in lines if line]
    if len(lines) > MAX_LINES:
        lines = [*lines[: MAX_LINES - 1], " ".join(lines[MAX_LINES - 1 :])]
    return "\n".join(lines)


def tenant_price_note(tenant: Any) -> str:
    """The tenant's note, or ``""`` when unset (or the tenant is ``None``)."""
    blob = getattr(tenant, "settings", None) if tenant is not None else None
    if not isinstance(blob, dict):
        return ""
    return normalize_price_note(blob.get(SETTINGS_KEY))[:MAX_LENGTH]
