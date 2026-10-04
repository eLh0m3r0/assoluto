"""Pricing 2026-10: new Starter / Pro prices and limits (CEO decision D3).

Revision ID: 1011_pricing_2026_10
Revises: 1008_order_assignment
Create Date: 2026-10-04

Packaging change from the 2026-10-03 audit (market.md §4, BIZ-14, MKT-2),
prices as finally set by the CEO on 2026-10-04:

* **Starter** 490 → 1 490 CZK / month, 3 staff users, storage 2 GB → 10 GB.
* **Pro** 1 490 → 2 990 CZK / month, staff 15 → 10, storage 20 GB → 50 GB.
* **Client contacts and orders are unlimited** on both paid plans
  (``NULL`` = unlimited, see :func:`app.platform.usage.ensure_within_limit`).
  Metering on the supplier's own clients punished exactly the behaviour
  the product needs — the more clients log in, the more value.

Rows are UPDATEd in place: codes and ids stay, so existing
``platform_subscriptions.plan_id`` foreign keys keep pointing at the same
plan. Founding customers keep the old 490 / 1 490 CZK by hand (D1) — there is no
per-subscription price in the schema, and Stripe price IDs live in env
(``STRIPE_PRICE_STARTER`` / ``STRIPE_PRICE_PRO``, CLAUDE.md §17), so the
operator must create new Stripe prices before switching billing on.

``downgrade()`` restores the values seeded by ``1003_billing`` (orders
stay NULL, as ``1006_drop_starter_orders_cap`` left them).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "1011_pricing_2026_10"
down_revision: str | Sequence[str] | None = "1008_order_assignment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (code, monthly_price_cents, max_users, max_contacts, max_orders_per_month, max_storage_mb)
NEW_VALUES = (
    ("starter", 149000, 3, None, None, 10240),
    ("pro", 299000, 10, None, None, 51200),
)

OLD_VALUES = (
    ("starter", 49000, 3, 20, None, 2048),
    ("pro", 149000, 15, 100, None, 20480),
)


def _sql_int(value: int | None) -> str:
    return "NULL" if value is None else str(int(value))


_Rows = tuple[tuple[str, int, int | None, int | None, int | None, int | None], ...]


def _apply(rows: _Rows) -> None:
    for code, price, users, contacts, orders, storage in rows:
        op.execute(
            "UPDATE platform_plans SET "
            f"monthly_price_cents = {_sql_int(price)}, "
            f"max_users = {_sql_int(users)}, "
            f"max_contacts = {_sql_int(contacts)}, "
            f"max_orders_per_month = {_sql_int(orders)}, "
            f"max_storage_mb = {_sql_int(storage)}, "
            "updated_at = now() "
            f"WHERE code = '{code}';"
        )


def upgrade() -> None:
    _apply(NEW_VALUES)


def downgrade() -> None:
    _apply(OLD_VALUES)
