"""Billing state machine columns on ``platform_subscriptions``.

Revision ID: 1012_billing_state
Revises: 1008_order_assignment
Create Date: 2026-10-04

The 2026-10-03 audit (theme T4, Codex-1/3/4/5/6/7/8) found that one row
per tenant with a single ``status`` string cannot tell apart the facts
the Stripe state machine needs. Every column is nullable and additive;
existing rows keep working with NULLs (documented per column).

``status_changed_at``
    When ``status`` last changed. Anchors the grace windows for
    non-paying Stripe states (``past_due``, ``unpaid``,
    ``incomplete``, ``incomplete_expired``, ``paused``) in the periodic
    entitlement job, and marks a manual ``active`` row as created by the
    new editor (which requires an explicit end date) — legacy manual
    ``active`` rows keep NULL and are therefore never auto-expired.

``canceled_at``
    First cancellation timestamp. Cancellation is idempotent: a second
    cancel must neither move it nor push the access end further out.

``stripe_subscription_created_at``
    ``created`` of the Stripe subscription we currently track. Events
    about an *older* subscription id (superseded generation) are
    ignored instead of overwriting the current one.

``stripe_last_event_at``
    ``created`` of the last Stripe event applied to the current
    subscription. An older event delivered late (Stripe guarantees
    delivery, not order) is ignored.

``pending_checkout_session_id``
    The one open Stripe Checkout session for this tenant. A new
    checkout expires the previous session first, so a tenant can never
    complete two checkouts and end up with two paid subscriptions.

``operator_suspended_at``
    Set when a platform admin deactivates the tenant. Billing recovery
    (a Stripe ``active`` update) may lift a *billing* cut but never an
    operator suspension. Pre-existing deactivations stay NULL — they
    cannot be told apart retroactively; the operator re-suspends if
    needed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1012_billing_state"
down_revision: str | Sequence[str] | None = "1013_growth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TS_COLUMNS = (
    "status_changed_at",
    "canceled_at",
    "stripe_subscription_created_at",
    "stripe_last_event_at",
    "operator_suspended_at",
)


def upgrade() -> None:
    for name in _TS_COLUMNS:
        op.add_column(
            "platform_subscriptions",
            sa.Column(name, sa.DateTime(timezone=True), nullable=True),
        )
    op.add_column(
        "platform_subscriptions",
        sa.Column("pending_checkout_session_id", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("platform_subscriptions", "pending_checkout_session_id")
    for name in reversed(_TS_COLUMNS):
        op.drop_column("platform_subscriptions", name)
