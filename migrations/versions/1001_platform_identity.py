"""Platform identity + tenant memberships.

Revision ID: 1001_platform_identity
Revises: 0007_assets
Create Date: 2026-04-11

Tables added here are intentionally NOT behind RLS — they live outside
the tenant boundary and are only touched by code in `app.platform`
(the hosted SaaS layer). They also exist in self-hosted / open-source
builds so a single schema works everywhere; the core app simply never
reads or writes them.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "1001_platform_identity"
down_revision: str | Sequence[str] | None = "0007_assets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "is_platform_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_platform_identities"),
        sa.UniqueConstraint("email", name="uq_platform_identities_email"),
    )
    op.create_index(
        "ix_platform_identities_email",
        "platform_identities",
        ["email"],
        unique=False,
    )

    op.create_table(
        "platform_tenant_memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("identity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
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
        sa.PrimaryKeyConstraint("id", name="pk_platform_tenant_memberships"),
        sa.ForeignKeyConstraint(
            ["identity_id"],
            ["platform_identities.id"],
            name="fk_platform_tm_identity_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_platform_tm_tenant_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_platform_tm_user_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["customer_contacts.id"],
            name="fk_platform_tm_contact_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "identity_id",
            "tenant_id",
            "user_id",
            "contact_id",
            name="uq_platform_tm_identity_tenant_targets",
        ),
    )
    op.create_index(
        "ix_platform_tm_identity_id",
        "platform_tenant_memberships",
        ["identity_id"],
        unique=False,
    )
    op.create_index(
        "ix_platform_tm_tenant_id",
        "platform_tenant_memberships",
        ["tenant_id"],
        unique=False,
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'portal_app') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON platform_identities, platform_tenant_memberships
                    TO portal_app;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_platform_tm_tenant_id", table_name="platform_tenant_memberships"
    )
    op.drop_index(
        "ix_platform_tm_identity_id", table_name="platform_tenant_memberships"
    )
    op.drop_table("platform_tenant_memberships")
    op.drop_index("ix_platform_identities_email", table_name="platform_identities")
    op.drop_table("platform_identities")
