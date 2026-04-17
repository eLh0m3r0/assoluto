"""Per-customer order permission helpers.

Permissions are stored as a JSONB dict on `Customer.order_permissions`.
Missing keys default to True (permissive) — an empty dict means
"everything allowed". Staff always bypasses these checks; they only
constrain customer contacts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OrderPermissions:
    """Resolved flags for a specific customer."""

    can_add_items: bool = True
    can_use_catalog: bool = True
    can_set_prices: bool = True
    can_upload_files: bool = True

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> OrderPermissions:
        if not raw:
            return cls()
        return cls(
            can_add_items=bool(raw.get("can_add_items", True)),
            can_use_catalog=bool(raw.get("can_use_catalog", True)),
            can_set_prices=bool(raw.get("can_set_prices", True)),
            can_upload_files=bool(raw.get("can_upload_files", True)),
        )

    def to_dict(self) -> dict[str, bool]:
        return {
            "can_add_items": self.can_add_items,
            "can_use_catalog": self.can_use_catalog,
            "can_set_prices": self.can_set_prices,
            "can_upload_files": self.can_upload_files,
        }
