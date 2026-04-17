"""Add email verification + ToS acceptance timestamps to Identity.

Revision ID: 1002_identity_verification
Revises: 0008_customer_order_perms
Create Date: 2026-04-16

Self-signup flow needs:

- ``email_verified_at`` — set once the user clicks the verification link
  emailed to them. Unverified identities can still log in (so we can
  nag them) but they see a banner prompting them to verify.
- ``terms_accepted_at`` — captured on the signup form; required for
  account creation. Never NULL for new rows, but nullable here so we
  can add the column without backfilling existing identities created
  before self-signup (platform admins + invited staff).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1002_identity_verification"
down_revision: str | Sequence[str] | None = "0008_customer_order_perms"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "platform_identities",
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "platform_identities",
        sa.Column("terms_accepted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("platform_identities", "terms_accepted_at")
    op.drop_column("platform_identities", "email_verified_at")
