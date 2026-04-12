"""Orders, order items, status history, comments.

Revision ID: 0004_orders
Revises: 0003_enable_rls
Create Date: 2026-04-11

Also enables RLS with the standard `tenant_isolation` policy and grants
SELECT/INSERT/UPDATE/DELETE on the new tables to `portal_app`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_orders"
down_revision: str | Sequence[str] | None = "0003_enable_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


RLS_TABLES: tuple[str, ...] = (
    "orders",
    "order_items",
    "order_status_history",
    "order_comments",
)


def upgrade() -> None:
    op.create_table(
        "orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("number", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_by_contact_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("requested_delivery_at", sa.Date(), nullable=True),
        sa.Column("promised_delivery_at", sa.Date(), nullable=True),
        sa.Column("quoted_total", sa.Numeric(12, 2), nullable=True),
        sa.Column(
            "currency", sa.String(length=8), nullable=False, server_default="CZK"
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_orders"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_orders_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name="fk_orders_customer_id_customers",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_orders_created_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_contact_id"],
            ["customer_contacts.id"],
            name="fk_orders_created_by_contact_id_customer_contacts",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("tenant_id", "number", name="uq_orders_tenant_id_number"),
    )
    op.create_index("ix_orders_tenant_id", "orders", ["tenant_id"], unique=False)
    op.create_index("ix_orders_customer_id", "orders", ["customer_id"], unique=False)
    op.create_index(
        "ix_orders_tenant_id_status", "orders", ["tenant_id", "status"], unique=False
    )
    op.create_index(
        "ix_orders_tenant_id_customer_id_created_at",
        "orders",
        ["tenant_id", "customer_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "order_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("description", sa.String(length=2000), nullable=False),
        sa.Column("quantity", sa.Numeric(12, 3), nullable=False),
        sa.Column("unit", sa.String(length=16), nullable=False, server_default="ks"),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("line_total", sa.Numeric(12, 2), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_order_items"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_order_items_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_order_items_order_id_orders",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_order_items_tenant_id", "order_items", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_order_items_order_id", "order_items", ["order_id"], unique=False
    )
    op.create_index(
        "ix_order_items_product_id", "order_items", ["product_id"], unique=False
    )

    op.create_table(
        "order_status_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("changed_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "changed_by_contact_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("note", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_order_status_history"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_order_status_history_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_order_status_history_order_id_orders",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["changed_by_user_id"],
            ["users.id"],
            name="fk_order_status_history_changed_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["changed_by_contact_id"],
            ["customer_contacts.id"],
            name="fk_order_status_history_changed_by_contact_id_customer_contacts",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_order_status_history_tenant_id",
        "order_status_history",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_order_status_history_order_id",
        "order_status_history",
        ["order_id"],
        unique=False,
    )

    op.create_table(
        "order_comments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("author_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("author_contact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "is_internal", sa.Boolean(), nullable=False, server_default=sa.false()
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
        sa.PrimaryKeyConstraint("id", name="pk_order_comments"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_order_comments_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_order_comments_order_id_orders",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["author_user_id"],
            ["users.id"],
            name="fk_order_comments_author_user_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["author_contact_id"],
            ["customer_contacts.id"],
            name="fk_order_comments_author_contact_id_customer_contacts",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_order_comments_tenant_id", "order_comments", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_order_comments_order_id", "order_comments", ["order_id"], unique=False
    )

    # Grant runtime privileges to portal_app (idempotent).
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'portal_app') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON orders, order_items, order_status_history, order_comments
                    TO portal_app;
            END IF;
        END $$;
        """
    )

    # Enable RLS + tenant_isolation policy on each new table.
    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
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
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")

    op.drop_index("ix_order_comments_order_id", table_name="order_comments")
    op.drop_index("ix_order_comments_tenant_id", table_name="order_comments")
    op.drop_table("order_comments")

    op.drop_index(
        "ix_order_status_history_order_id", table_name="order_status_history"
    )
    op.drop_index(
        "ix_order_status_history_tenant_id", table_name="order_status_history"
    )
    op.drop_table("order_status_history")

    op.drop_index("ix_order_items_product_id", table_name="order_items")
    op.drop_index("ix_order_items_order_id", table_name="order_items")
    op.drop_index("ix_order_items_tenant_id", table_name="order_items")
    op.drop_table("order_items")

    op.drop_index(
        "ix_orders_tenant_id_customer_id_created_at", table_name="orders"
    )
    op.drop_index("ix_orders_tenant_id_status", table_name="orders")
    op.drop_index("ix_orders_customer_id", table_name="orders")
    op.drop_index("ix_orders_tenant_id", table_name="orders")
    op.drop_table("orders")
