"""Money and quantity validation shared by routers and services.

One place decides what a valid price or quantity is (audit 2026-10-03
LOGIC-1, CEO decision D4). Before this, every route did a bare
``Decimal(raw)``: ``Decimal("NaN")``, ``Decimal("Infinity")`` and
negatives all passed, Postgres ``numeric`` happily stored ``'NaN'``, and
the next render of the supplier's order list died on ``int(NaN)``. One
customer POST could take the main staff screen down.

Rules:

* finite numbers only — no ``NaN`` / ``Infinity``;
* prices ``>= 0`` (no negative "discount" lines — D4), quantities ``> 0``;
* magnitude must fit the column: ``Numeric(12, 2)`` for money,
  ``Numeric(12, 3)`` for quantities. An out-of-range value would
  otherwise surface as a ``DataError`` (500) at flush time;
* values are rounded half-up to the column scale, matching Czech/German
  commercial practice (``0.125 -> 0.13``) rather than Python's default
  banker's rounding.

Routers call :func:`parse_money` / :func:`parse_quantity` on the raw form
string and turn :class:`AmountError` into a friendly flash; services call
:func:`check_money` / :func:`check_quantity` again as defence in depth,
so a caller that skips the router (a script, a future API) still cannot
write a poisoned row.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

#: ``Numeric(12, 2)`` holds at most 10 integer digits.
MONEY_MAX = Decimal("9999999999.99")
#: ``Numeric(12, 3)`` holds at most 9 integer digits.
QUANTITY_MAX = Decimal("999999999.999")

MONEY_STEP = Decimal("0.01")
QUANTITY_STEP = Decimal("0.001")


class AmountError(ValueError):
    """A price or quantity failed validation.

    ``kind`` is ``"price"`` or ``"quantity"``; ``reason`` is one of
    ``"invalid"``, ``"negative"``, ``"zero"``, ``"too_large"``. Routers map
    the pair to a translated message — the exception itself carries no
    user-facing text so it stays usable outside a request.
    """

    def __init__(self, kind: str, reason: str) -> None:
        super().__init__(f"{kind}: {reason}")
        self.kind = kind
        self.reason = reason


def round_money(value: Decimal) -> Decimal:
    """Quantise to 2 decimals, half-up."""
    return Decimal(value).quantize(MONEY_STEP, rounding=ROUND_HALF_UP)


def _to_decimal(raw: object, kind: str) -> Decimal:
    if isinstance(raw, Decimal):
        value = raw
    else:
        text = str(raw).strip().replace("\N{NO-BREAK SPACE}", "").replace(" ", "")
        # Czech and German users type a decimal comma. ``<input
        # type="number">`` normalises to a dot, but a pasted value or a
        # non-JS client may not.
        if text.count(",") == 1 and "." not in text:
            text = text.replace(",", ".")
        try:
            value = Decimal(text)
        except (InvalidOperation, ValueError):
            raise AmountError(kind, "invalid") from None
    if not value.is_finite():
        raise AmountError(kind, "invalid")
    return value


def check_money(value: Decimal) -> Decimal:
    """Validate an already-parsed price; return it rounded to 2 decimals."""
    value = _to_decimal(value, "price")
    if value < 0:
        raise AmountError("price", "negative")
    try:
        rounded = round_money(value)
    except InvalidOperation:
        raise AmountError("price", "too_large") from None
    if rounded > MONEY_MAX:
        raise AmountError("price", "too_large")
    return rounded


def check_quantity(value: Decimal) -> Decimal:
    """Validate an already-parsed quantity; return it rounded to 3 decimals."""
    value = _to_decimal(value, "quantity")
    try:
        rounded = value.quantize(QUANTITY_STEP, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        raise AmountError("quantity", "too_large") from None
    if rounded <= 0:
        raise AmountError("quantity", "zero" if value >= 0 else "negative")
    if rounded > QUANTITY_MAX:
        raise AmountError("quantity", "too_large")
    return rounded


def parse_money(raw: str | None) -> Decimal | None:
    """Parse a form price. Blank means "no price" and returns ``None``."""
    if raw is None or not str(raw).strip():
        return None
    return check_money(_to_decimal(raw, "price"))


def parse_quantity(raw: str | None) -> Decimal:
    """Parse a form quantity. Blank is an error — a line needs a quantity."""
    if raw is None or not str(raw).strip():
        raise AmountError("quantity", "invalid")
    return check_quantity(_to_decimal(raw, "quantity"))


def line_total(quantity: Decimal, unit_price: Decimal | None) -> Decimal | None:
    """``quantity * unit_price`` rounded half-up, or ``None`` if unpriced.

    Raises :class:`AmountError` (``price`` / ``too_large``) when the
    product does not fit the ``line_total`` column — both factors can be
    in range while their product is not.
    """
    if unit_price is None:
        return None
    total = round_money(Decimal(quantity) * Decimal(unit_price))
    if total > MONEY_MAX:
        raise AmountError("price", "too_large")
    return total
