"""Add orders.assigned_to_user_id (order ownership).

Revision ID: 1008_order_assignment
Revises: 1007_identity_session_version
Create Date: 2026-08-19

Backs the notification redesign (``docs/NOTIFICATIONS_REDESIGN_2026-08-19.md``).
Until now every staff-side notification was a broadcast; with an owner on
the row a recipient can set their scope to ``involved`` and hear only
about their own work.

``ON DELETE SET NULL`` rather than RESTRICT: removing a staff member must
not be blocked by their open orders, and an unassigned order is a valid,
visible state that the "Unassigned" list filter surfaces for reassignment.

The column is nullable and unset for every existing row — orders written
before this migration stay unassigned, and the ``all`` default scope means
their notifications keep going to everyone exactly as before.

No preferences migration is needed: ``users.notification_prefs`` and
``customer_contacts.notification_prefs`` already exist (``0002``), and
:class:`app.services.notification_prefs.NotificationPrefs` reads a missing
key as the role default, so the pre-existing ``{}`` rows resolve correctly
without a backfill.

No RLS changes: ``orders`` already has a tenant policy, and the FK target
(``users``) is tenant-scoped by its own policy.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1008_order_assignment"
down_revision: str | Sequence[str] | None = "1007_identity_session_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("assigned_to_user_id", sa.Uuid(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_orders_assigned_to_user_id",
        "orders",
        "users",
        ["assigned_to_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Composite with tenant_id because every query is already RLS-scoped;
    # a bare index on the FK would not be selective enough to be used.
    op.create_index(
        "ix_orders_tenant_id_assigned_to",
        "orders",
        ["tenant_id", "assigned_to_user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_orders_tenant_id_assigned_to", table_name="orders")
    op.drop_constraint("fk_orders_assigned_to_user_id", "orders", type_="foreignkey")
    op.drop_column("orders", "assigned_to_user_id")
