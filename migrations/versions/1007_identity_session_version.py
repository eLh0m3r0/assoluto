"""Platform identity session_version + customer contact last_login_at.

Two unrelated-looking columns, added together because both close gaps
found in the 2026-07-26 audit and both are pure additive columns.

``platform_identities.session_version``
    The platform half of CLAUDE.md §14 was missing. Tenant password
    resets embed the principal's ``session_version`` in the token and
    bump it on consume, so a reset link cannot be replayed and existing
    sessions die. Platform resets had no such column: the token carried
    only ``identity_id``, so anyone holding the URL could replay it for
    the full 30-minute TTL, and a completed reset left every existing
    platform session logged in. Mirrors ``users.session_version``.

``customer_contacts.last_login_at``
    ``users`` has had this since 0002; contacts never did, so neither
    the supplier nor the operator could answer "has this client ever
    actually logged in?" — the one number that says whether a customer
    portal is working at all.

Revision ID: 1007_identity_session_version
Revises: 1006_drop_starter_orders_cap
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1007_identity_session_version"
down_revision: str | Sequence[str] | None = "1006_drop_starter_orders_cap"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "platform_identities",
        sa.Column(
            "session_version",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "customer_contacts",
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("customer_contacts", "last_login_at")
    op.drop_column("platform_identities", "session_version")
