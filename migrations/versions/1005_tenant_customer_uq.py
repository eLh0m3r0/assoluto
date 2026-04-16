"""Harden the Stripe customer link on tenants.

Revision ID: 1005_tenant_customer_unique
Revises: 1004_stripe_events
Create Date: 2026-04-16

Second-round audit follow-up. Two hardening bumps:

1. Add a partial UNIQUE index on ``tenants.stripe_customer_id``
   (``WHERE stripe_customer_id IS NOT NULL``). Two tenants must never
   share a Stripe Customer; the ``_resolve_tenant_id`` fallback chain
   would otherwise have to disambiguate and could pick the wrong row.

2. Drop the old non-unique index added in ``1004_stripe_events`` — the
   new UNIQUE index replaces it for lookup purposes.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "1005_tenant_customer_uq"
down_revision: str | Sequence[str] | None = "1004_stripe_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_tenants_stripe_customer_id", table_name="tenants")
    # Partial unique — NULLs (trial tenants without a Stripe customer yet)
    # don't collide; at most one tenant can hold a given customer id.
    op.create_index(
        "uq_tenants_stripe_customer_id",
        "tenants",
        ["stripe_customer_id"],
        unique=True,
        postgresql_where="stripe_customer_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_index("uq_tenants_stripe_customer_id", table_name="tenants")
    op.create_index(
        "ix_tenants_stripe_customer_id",
        "tenants",
        ["stripe_customer_id"],
        unique=False,
    )
