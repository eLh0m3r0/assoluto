"""Add customers.weekly_summary_enabled (weekly open-orders summary, IDEA-10).

Revision ID: 1013_growth
Revises: 1008_order_assignment
Create Date: 2026-10-04

Per-customer opt-in for the Monday "your open orders" email to the
customer's admin contacts. Off for every existing and new customer: the
supplier switches it on per customer on the customer edit form. Each
recipient can still opt out on their own profile (notification event
``weekly_summary`` — consent is absolute, CLAUDE.md §19).

No RLS change: ``customers`` already carries a tenant policy.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1013_growth"
down_revision: str | Sequence[str] | None = "1009_order_quote_integrity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "customers",
        sa.Column(
            "weekly_summary_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("customers", "weekly_summary_enabled")
