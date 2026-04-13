"""Add order_permissions JSONB column to customers.

Revision ID: 0008_customer_order_perms
Revises: 1001_platform_identity
Create Date: 2026-04-13

Stores per-customer flags controlling what contacts can do in orders:
can_add_items, can_use_catalog, can_set_prices, can_upload_files.
Empty dict = all permissions granted (backwards-compatible default).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_customer_order_perms"
down_revision: str | Sequence[str] | None = "1001_platform_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "customers",
        sa.Column(
            "order_permissions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("customers", "order_permissions")
