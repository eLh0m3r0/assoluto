"""Add access_type column to platform_tenant_memberships.

Revision ID: 0011_membership_access_type
Revises: 0010_orders_delivered_at
Create Date: 2026-04-22

Distinguishes a regular team / contact membership ('member') from an
opt-in support attachment created by a platform admin through
``/platform/admin/tenants/{id}/support-access`` ('support').

The distinction lets:
- ``/platform/select-tenant`` render a badge explaining HOW the
  identity got there.
- ``/platform/admin/tenants`` show the platform admin whether they
  already have support access to each tenant + offer a revoke action.
- Audit queries filter for support-access grants / revocations.

Default ``'member'`` — every existing row back-fills to normal team
membership since support access didn't exist before this release.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_membership_access_type"
down_revision: str | Sequence[str] | None = "0010_orders_delivered_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "platform_tenant_memberships",
        sa.Column(
            "access_type",
            sa.String(length=16),
            nullable=False,
            server_default="member",
        ),
    )
    op.create_index(
        "ix_platform_tm_access_type",
        "platform_tenant_memberships",
        ["access_type"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_platform_tm_access_type", table_name="platform_tenant_memberships")
    op.drop_column("platform_tenant_memberships", "access_type")
