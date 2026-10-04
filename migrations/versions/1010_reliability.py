"""Reliability: durable e-mail outbox + tenant deactivation timestamp.

Revision ID: 1010_reliability
Revises: 1008_order_assignment
Create Date: 2026-10-04

Two independent pieces, one revision (audit 2026-10-03, team backend):

``email_outbox`` (BE-09)
    Every templated mail is written here before the first send attempt,
    so an SMTP outage or a deploy no longer loses it. Rows are claimed
    with ``FOR UPDATE SKIP LOCKED`` by the inline sender and by the
    ``deliver_email_outbox`` job. Owner-only on purpose: the rendering
    context can carry one-shot reset / invitation URLs, so ``portal_app``
    gets no grant (the app writes it through the owner DSN). Not
    tenant-scoped (platform mails have no tenant), hence no RLS; the
    nullable ``tenant_id`` is informational and cascades on tenant delete.

``tenants.deactivated_at`` (BIZ-08 / D6)
    The retention job deletes a deactivated tenant's data 30 days after
    deactivation, so it needs to know *when* that happened. A trigger
    stamps the column whenever ``is_active`` flips to false (and clears
    it on reactivation), which covers every writer — the admin UI, the
    platform service and the raw-SQL billing job — without touching
    them. Existing inactive tenants are backfilled from ``updated_at``,
    the best available approximation.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "1010_reliability"
down_revision: str | Sequence[str] | None = "1008_order_assignment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "email_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("template", sa.String(64), nullable=False),
        sa.Column("to_address", sa.String(320), nullable=False),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("locale", sa.String(16), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # The retry job's only query: pending rows by due time.
    op.create_index(
        "ix_email_outbox_pending",
        "email_outbox",
        ["next_attempt_at"],
        postgresql_where=sa.text("sent_at IS NULL AND failed_at IS NULL"),
    )

    op.add_column(
        "tenants",
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE tenants SET deactivated_at = COALESCE(updated_at, now()) WHERE is_active = false"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION tenants_stamp_deactivated_at() RETURNS trigger AS $$
        BEGIN
            IF NEW.is_active THEN
                NEW.deactivated_at := NULL;
            ELSIF TG_OP = 'INSERT' OR OLD.is_active THEN
                NEW.deactivated_at := COALESCE(NEW.deactivated_at, now());
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_tenants_deactivated_at
        BEFORE INSERT OR UPDATE OF is_active ON tenants
        FOR EACH ROW EXECUTE FUNCTION tenants_stamp_deactivated_at();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_tenants_deactivated_at ON tenants")
    op.execute("DROP FUNCTION IF EXISTS tenants_stamp_deactivated_at()")
    op.drop_column("tenants", "deactivated_at")
    op.drop_index("ix_email_outbox_pending", table_name="email_outbox")
    op.drop_table("email_outbox")
