"""Billing tables: plans, subscriptions, invoices.

Revision ID: 1003_billing
Revises: 1002_identity_verification
Create Date: 2026-04-16

The billing model runs alongside the platform layer:

* ``platform_plans``        — catalogue of plans (seeded by this migration)
* ``platform_subscriptions`` — one per tenant (at most), tracks active plan + status
* ``platform_invoices``      — cached from Stripe webhooks, for in-app invoice history

All three are platform-owned (no RLS) — they're only touched by code in
``app.platform.billing`` via the owner DB role. A self-hosted deployment
will never reach these tables because ``FEATURE_PLATFORM=false`` prevents
the billing router from being mounted.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "1003_billing"
down_revision: str | Sequence[str] | None = "1002_identity_verification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),  # "community", "starter", "pro"
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("stripe_price_id", sa.String(length=128), nullable=True),
        sa.Column("monthly_price_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="CZK"),
        sa.Column("max_users", sa.Integer(), nullable=True),  # NULL = unlimited
        sa.Column("max_contacts", sa.Integer(), nullable=True),
        sa.Column("max_orders_per_month", sa.Integer(), nullable=True),
        sa.Column("max_storage_mb", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_platform_plans"),
        sa.UniqueConstraint("code", name="uq_platform_plans_code"),
    )

    op.create_table(
        "platform_subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stripe_customer_id", sa.String(length=128), nullable=True),
        sa.Column("stripe_subscription_id", sa.String(length=128), nullable=True),
        # "trialing", "active", "past_due", "canceled", "incomplete", "demo"
        sa.Column("status", sa.String(length=32), nullable=False, server_default="trialing"),
        sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_platform_subscriptions"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_subs_tenant_id", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["platform_plans.id"], name="fk_subs_plan_id", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("tenant_id", name="uq_subs_tenant_id"),
    )

    op.create_table(
        "platform_invoices",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stripe_invoice_id", sa.String(length=128), nullable=True),
        sa.Column("number", sa.String(length=64), nullable=True),
        sa.Column("amount_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="CZK"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="draft"),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hosted_invoice_url", sa.String(length=512), nullable=True),
        sa.Column("pdf_url", sa.String(length=512), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_platform_invoices"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_inv_tenant_id", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("stripe_invoice_id", name="uq_inv_stripe_invoice_id"),
    )
    op.create_index(
        "ix_platform_invoices_tenant_id",
        "platform_invoices",
        ["tenant_id"],
        unique=False,
    )

    # Seed built-in plans so signup can attach a default immediately.
    op.execute(
        """
        INSERT INTO platform_plans
            (id, code, name, monthly_price_cents, currency,
             max_users, max_contacts, max_orders_per_month, max_storage_mb)
        VALUES
            (gen_random_uuid(), 'community', 'Community',     0,    'CZK', NULL, NULL, NULL, NULL),
            (gen_random_uuid(), 'starter',   'Starter',    49000, 'CZK', 3,    20,   100,  2048),
            (gen_random_uuid(), 'pro',       'Pro',       149000, 'CZK', 15,   100,  NULL, 20480),
            (gen_random_uuid(), 'enterprise','Enterprise',     0, 'CZK', NULL, NULL, NULL, NULL)
        ON CONFLICT (code) DO NOTHING;
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'portal_app') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON platform_plans, platform_subscriptions, platform_invoices
                    TO portal_app;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.drop_index("ix_platform_invoices_tenant_id", table_name="platform_invoices")
    op.drop_table("platform_invoices")
    op.drop_table("platform_subscriptions")
    op.drop_table("platform_plans")
