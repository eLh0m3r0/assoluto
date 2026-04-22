"""Add preferred_locale to users, customers, customer_contacts.

Revision ID: 0012_preferred_locale
Revises: 0011_membership_access_type
Create Date: 2026-04-22

The resolution chain for outbound-email locale is:

    1. recipient.preferred_locale       (user OR customer_contact)
    2. customer.preferred_locale         (for customer contacts)
    3. tenants.settings -> "default_locale" (per-tenant default)
    4. settings.default_locale           (app-wide default)

NULL means "inherit from parent" — no sentinel string, so we don't need
to pick a magic value here or migrate data if we later expand the set of
supported locales.

We keep the tenant default inside the existing ``tenants.settings`` JSONB
blob rather than adding a dedicated column; tenant admins mostly flip
this once on setup and settings is already the established home for
non-essential per-tenant configuration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_preferred_locale"
down_revision: str | Sequence[str] | None = "0011_membership_access_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("preferred_locale", sa.String(length=8), nullable=True),
    )
    op.add_column(
        "customers",
        sa.Column("preferred_locale", sa.String(length=8), nullable=True),
    )
    op.add_column(
        "customer_contacts",
        sa.Column("preferred_locale", sa.String(length=8), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("customer_contacts", "preferred_locale")
    op.drop_column("customers", "preferred_locale")
    op.drop_column("users", "preferred_locale")
