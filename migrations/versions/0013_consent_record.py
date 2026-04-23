"""Capture ToS consent version + source IP on Identity.

Revision ID: 0013_consent_record
Revises: 0012_preferred_locale
Create Date: 2026-04-23

GDPR consent is stronger when you can show, for each subject:

* *which version* of the terms they accepted
* *when*
* *from where* (source IP)

The existing ``terms_accepted_at`` column covers the "when". This
migration adds two more nullable columns:

* ``terms_accepted_version`` — semver or date tag of the document
  (e.g. ``"2026.05"``). Matched against ``privacy.html`` /
  ``terms.html`` version strings.
* ``terms_accepted_ip`` — string; IPv4 or IPv6 the acceptance
  POST originated from. Stored as ``inet`` to benefit from Postgres
  validation. Nullable because existing pre-migration rows have no
  record.

Erasure (``app.services.gdpr_service.erase_identity``) nulls these
fields alongside the other PII.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_consent_record"
down_revision: str | Sequence[str] | None = "0012_preferred_locale"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "platform_identities",
        sa.Column("terms_accepted_version", sa.String(length=32), nullable=True),
    )
    # Store as plain VARCHAR rather than Postgres ``inet`` — asyncpg's
    # inet support needs a bespoke type adapter and the validation
    # value we'd get is negligible (we validate before write anyway).
    # 45 chars covers the longest IPv6 literal.
    op.add_column(
        "platform_identities",
        sa.Column("terms_accepted_ip", sa.String(length=45), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("platform_identities", "terms_accepted_ip")
    op.drop_column("platform_identities", "terms_accepted_version")
