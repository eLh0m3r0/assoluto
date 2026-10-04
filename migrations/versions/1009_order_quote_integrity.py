"""Order quote integrity: confirmation snapshot, quote timestamps, reminders.

Revision ID: 1009_order_quote_integrity
Revises: 1008_order_assignment
Create Date: 2026-10-04

Backs the 2026-10-03 audit fixes LOGIC-2 / IDEA-2 / IDEA-4:

* ``confirmed_total`` / ``confirmed_at`` / ``confirmed_by_user_id`` /
  ``confirmed_by_contact_id`` — a snapshot of *what was agreed*, written
  when the order lands on CONFIRMED. Until now the only record of the
  accepted amount was the live ``quoted_total`` cache, which is re-summed
  on every item edit.
* ``quoted_at`` — when the order last entered QUOTED. Drives the "quotes
  waiting for the customer" queue and the quote follow-up reminder.
* ``quote_reminder_sent_at`` — idempotency marker for the reminder job;
  a reminder is due only when it is older than ``quoted_at``, so a
  re-quote re-arms it automatically.
* ``ix_orders_tenant_id_promised_delivery_at`` — the order list is now
  sortable by promised date and the dashboard counts overdue orders.

All columns are nullable with no backfill: existing confirmed orders keep
a NULL snapshot (the UI falls back to the live total), and existing
quoted orders get no reminder until they are re-quoted.

No RLS changes — ``orders`` already carries a tenant policy, and the FK
targets (``users``, ``customer_contacts``) are tenant-scoped by theirs.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1009_order_quote_integrity"
down_revision: str | Sequence[str] | None = "1011_pricing_2026_10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("confirmed_total", sa.Numeric(12, 2), nullable=True))
    op.add_column(
        "orders", sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "orders", sa.Column("confirmed_by_user_id", sa.Uuid(as_uuid=True), nullable=True)
    )
    op.add_column(
        "orders", sa.Column("confirmed_by_contact_id", sa.Uuid(as_uuid=True), nullable=True)
    )
    op.add_column("orders", sa.Column("quoted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "orders",
        sa.Column("quote_reminder_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_orders_confirmed_by_user_id",
        "orders",
        "users",
        ["confirmed_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_orders_confirmed_by_contact_id",
        "orders",
        "customer_contacts",
        ["confirmed_by_contact_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_orders_tenant_id_promised_delivery_at",
        "orders",
        ["tenant_id", "promised_delivery_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_orders_tenant_id_promised_delivery_at", table_name="orders")
    op.drop_constraint("fk_orders_confirmed_by_contact_id", "orders", type_="foreignkey")
    op.drop_constraint("fk_orders_confirmed_by_user_id", "orders", type_="foreignkey")
    op.drop_column("orders", "quote_reminder_sent_at")
    op.drop_column("orders", "quoted_at")
    op.drop_column("orders", "confirmed_by_contact_id")
    op.drop_column("orders", "confirmed_by_user_id")
    op.drop_column("orders", "confirmed_at")
    op.drop_column("orders", "confirmed_total")
