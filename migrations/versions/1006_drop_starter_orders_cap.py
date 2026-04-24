"""Drop the 100-orders/month cap on Starter — unlimited orders for every plan.

Revision ID: 1006_drop_starter_orders_cap
Revises: 1005_tenant_customer_uq
Create Date: 2026-04-24

Product decision: we don't want to cap any plan on monthly order
volume. The 100/month limit on Starter made the free tier feel
artificially cramped for the very small shops who are the ideal
entry persona (12 employees, ~5 B2B clients, ~30 orders / month,
but with spikes). The other limits (users, contacts, storage_mb)
already do the work of tier-gating.

We simply NULL out ``max_orders_per_month`` on every existing plan
row. Future plans seeded via the app should also leave this field
NULL unless a hard reason emerges.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "1006_drop_starter_orders_cap"
# The chain is already merged at 0010 (parents 0009 + 1005), so the
# current single head is 0013. Attach here so we stay single-head.
down_revision: str | Sequence[str] | None = "0013_consent_record"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE platform_plans SET max_orders_per_month = NULL;")


def downgrade() -> None:
    op.execute(
        """
        UPDATE platform_plans SET max_orders_per_month = 100 WHERE code = 'starter';
        """
    )
