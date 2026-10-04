"""Renumber order item positions.

``add_item`` computed the next position as ``int(max_pos or -1) + 1``, so
a first line at position 0 turned the next ``MAX`` into ``-1 + 1`` again:
every line of every order got position 0 (since 2026-04). Display order
fell back to ``created_at``; exports and anything sorting by position
alone were non-deterministic.

Renumbers each order's lines 0..n-1 in the order they have been shown
all along (position, created_at, id). Downgrade is a no-op: the old
values carried no information beyond that order.

Revision ID: 1014_renumber_item_positions
Revises: 1012_billing_state
"""

from collections.abc import Sequence

from alembic import op

revision: str = "1014_renumber_item_positions"
down_revision: str | Sequence[str] | None = "1012_billing_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE order_items AS i
           SET position = r.rn
          FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY order_id ORDER BY position, created_at, id
                       ) - 1 AS rn
                  FROM order_items
               ) AS r
         WHERE i.id = r.id
           AND i.position IS DISTINCT FROM r.rn
        """
    )


def downgrade() -> None:
    pass
