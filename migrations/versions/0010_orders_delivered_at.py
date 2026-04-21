"""Add nullable delivered_at Date column to orders (SLA tracking).

Revision ID: 0010_orders_delivered_at
Revises: 0009_audit_events, 1005_tenant_customer_uq
Create Date: 2026-04-21

Feeds the SLA service (``app.services.sla_service``) which compares
``delivered_at`` against ``promised_delivery_at`` to compute on-time
delivery rates. The column is nullable by design — historical orders
without a recorded delivery date are treated as "not yet delivered"
rather than back-filled (see plan §9, option b).

Doubles as a merge migration unifying the two revision heads that
existed at merge time: the core audit-events chain (``0009_audit_events``)
and the platform chain (``1005_tenant_customer_uq``).

No RLS changes: ``orders`` already has a tenant policy.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_orders_delivered_at"
down_revision: str | Sequence[str] | None = (
    "0009_audit_events",
    "1005_tenant_customer_uq",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("delivered_at", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orders", "delivered_at")
