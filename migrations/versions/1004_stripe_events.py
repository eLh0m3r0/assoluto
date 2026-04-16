"""Stripe event dedup table + tenants.stripe_customer_id column.

Revision ID: 1004_stripe_events
Revises: 1003_billing
Create Date: 2026-04-16

Two related changes rolled into one migration:

1. ``platform_stripe_events`` — receipt log for incoming webhook events,
   keyed on ``event.id``. The webhook handler inserts on arrival with
   ``ON CONFLICT DO NOTHING``; a duplicate delivery turns into a cheap
   200 OK with no side effects.

2. ``tenants.stripe_customer_id`` — persisted the Stripe Customer id on
   the Tenant (not the Subscription). Customers outlive subscriptions
   — a tenant who cancels and later resubscribes should reuse their
   Stripe Customer so history and saved payment methods carry over.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1004_stripe_events"
down_revision: str | Sequence[str] | None = "1003_billing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_stripe_events",
        sa.Column("id", sa.String(length=255), nullable=False),
        sa.Column("type", sa.String(length=128), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_platform_stripe_events"),
    )

    op.add_column(
        "tenants",
        sa.Column("stripe_customer_id", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "ix_tenants_stripe_customer_id",
        "tenants",
        ["stripe_customer_id"],
        unique=False,
    )

    # ``portal_app`` never reads the dedup table — only the platform
    # owner session does. Explicit grant for symmetry with 1003.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'portal_app') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON platform_stripe_events TO portal_app;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.drop_index("ix_tenants_stripe_customer_id", table_name="tenants")
    op.drop_column("tenants", "stripe_customer_id")
    op.drop_table("platform_stripe_events")
