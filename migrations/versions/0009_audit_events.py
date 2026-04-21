"""Audit events — append-only log of user + system actions.

Revision ID: 0009_audit_events
Revises: 0008_customer_order_perms
Create Date: 2026-04-21

Adds the ``audit_events`` table that backs §6 of the Sprint-3 plan
(audit log) and §7 (recent activity feed, built on top in a later PR).

Design choices worth remembering:

- The actor reference is **polymorphic** across ``users`` and
  ``customer_contacts``, with a third ``system`` variant for jobs that
  run without a human in the loop. Rather than two nullable FKs we use
  ``actor_type`` + ``actor_id`` + a denormalised ``actor_label`` that
  survives the target row being deleted.
- Writes happen in the caller's transaction — atomicity with the
  business mutation is part of the contract. Reads go through RLS like
  everything else.
- Append-only: ``portal_app`` is granted SELECT + INSERT; no UPDATE,
  no DELETE. Admins fix mistakes by adding compensating events.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_audit_events"
down_revision: str | Sequence[str] | None = "0008_customer_order_perms"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


RLS_TABLES: tuple[str, ...] = ("audit_events",)


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Polymorphic actor — no FK so the row survives deletion of the
        # underlying user/contact (and so 'system' events don't need a
        # synthetic principal row).
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_label", sa.String(length=255), nullable=False),
        # Canonical action code, e.g. ``order.status_changed``.
        sa.Column("action", sa.String(length=64), nullable=False),
        # Affected entity — loose shape so new domains plug in without a
        # migration. Current entity_type values: order, customer,
        # product, user.
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_label", sa.String(length=255), nullable=False),
        # Free-form JSON payload. Convention: ``{"before": {...},
        # "after": {...}}`` for updates. Nullable so create/delete
        # events don't need a placeholder.
        sa.Column("diff", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        # Optional correlation with structured logs.
        sa.Column("request_id", sa.String(length=64), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_audit_events_tenant_id_tenants",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_audit_events_tenant_id", "audit_events", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_audit_events_tenant_id_occurred_at",
        "audit_events",
        ["tenant_id", sa.text("occurred_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_audit_events_tenant_entity",
        "audit_events",
        ["tenant_id", "entity_type", "entity_id"],
        unique=False,
    )

    # Append-only: SELECT + INSERT only. No UPDATE, no DELETE — admins
    # must add compensating events if a row is wrong.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'portal_app') THEN
                GRANT SELECT, INSERT ON audit_events TO portal_app;
            END IF;
        END $$;
        """
    )

    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
                USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
                WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
            """
        )


def downgrade() -> None:
    for table in RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table};")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")

    op.drop_index("ix_audit_events_tenant_entity", table_name="audit_events")
    op.drop_index("ix_audit_events_tenant_id_occurred_at", table_name="audit_events")
    op.drop_index("ix_audit_events_tenant_id", table_name="audit_events")
    op.drop_table("audit_events")
