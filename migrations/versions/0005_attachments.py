"""Order attachments table.

Revision ID: 0005_attachments
Revises: 0004_orders
Create Date: 2026-04-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_attachments"
down_revision: str | Sequence[str] | None = "0004_orders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "order_attachments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_item_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "kind",
            sa.String(length=16),
            nullable=False,
            server_default="document",
        ),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("thumbnail_key", sa.String(length=512), nullable=True),
        sa.Column("uploaded_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "uploaded_by_contact_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
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
        sa.PrimaryKeyConstraint("id", name="pk_order_attachments"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_order_attachments_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_order_attachments_order_id_orders",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["order_item_id"],
            ["order_items.id"],
            name="fk_order_attachments_order_item_id_order_items",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by_user_id"],
            ["users.id"],
            name="fk_order_attachments_uploaded_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by_contact_id"],
            ["customer_contacts.id"],
            name="fk_order_attachments_uploaded_by_contact_id_customer_contacts",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_order_attachments_tenant_id",
        "order_attachments",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_order_attachments_order_id",
        "order_attachments",
        ["order_id"],
        unique=False,
    )
    op.create_index(
        "ix_order_attachments_order_item_id",
        "order_attachments",
        ["order_item_id"],
        unique=False,
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'portal_app') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON order_attachments TO portal_app;
            END IF;
        END $$;
        """
    )

    op.execute("ALTER TABLE order_attachments ENABLE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON order_attachments
            USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON order_attachments;")
    op.execute("ALTER TABLE order_attachments DISABLE ROW LEVEL SECURITY;")
    op.drop_index(
        "ix_order_attachments_order_item_id", table_name="order_attachments"
    )
    op.drop_index("ix_order_attachments_order_id", table_name="order_attachments")
    op.drop_index("ix_order_attachments_tenant_id", table_name="order_attachments")
    op.drop_table("order_attachments")
