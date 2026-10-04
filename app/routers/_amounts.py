"""Translate :class:`app.services.money.AmountError` into a user message.

Kept out of :mod:`app.services.money` because services never see a
``Request``. Every message is a literal ``_t(request, "...")`` call so
``pybabel extract -k _t:2`` picks it up (CLAUDE.md §7).
"""

from __future__ import annotations

from fastapi import Request

from app.i18n import t as _t
from app.services.money import AmountError


def amount_error_message(request: Request, exc: AmountError) -> str:
    if exc.kind == "quantity":
        if exc.reason == "too_large":
            return _t(request, "Quantity is too large.")
        if exc.reason in ("zero", "negative"):
            return _t(request, "Quantity must be greater than zero.")
        return _t(request, "Enter a valid quantity.")
    if exc.reason == "negative":
        return _t(request, "Price must not be negative.")
    if exc.reason == "too_large":
        return _t(request, "Amount is too large.")
    return _t(request, "Enter a valid price.")
