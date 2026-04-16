"""Platform-level (cross-tenant) ORM models.

These tables are intentionally NOT tenant-scoped — an `Identity` row
represents a single human being, regardless of how many tenants they
can access. `TenantMembership` links a global identity to a concrete
User or CustomerContact in a specific tenant.

None of these tables are protected by Row-Level Security: the
platform package runs as the `portal` role (table owner) via the
owner DSN, so RLS is bypassed automatically for tenant-owned tables
it touches.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class Identity(Base, TimestampMixin):
    """A globally unique person who can log in to the hosted platform.

    Email is the primary key at the identity layer — only one platform
    account per e-mail regardless of how many tenants it's linked to.
    """

    __tablename__ = "platform_identities"
    __table_args__ = (UniqueConstraint("email", name="uq_platform_identities_email"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)

    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    is_platform_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Self-signup lifecycle fields (see migration 1002).
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    terms_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class TenantMembership(Base, TimestampMixin):
    """Link between an Identity and a specific tenant.

    Exactly one of `user_id` / `contact_id` is set: either the identity
    is a tenant staff member OR a customer contact in this tenant.
    A single identity can have at most one membership of each kind per
    tenant.
    """

    __tablename__ = "platform_tenant_memberships"
    __table_args__ = (
        UniqueConstraint(
            "identity_id",
            "tenant_id",
            "user_id",
            "contact_id",
            name="uq_platform_tm_identity_tenant_targets",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)

    identity_id: Mapped[UUID] = mapped_column(
        ForeignKey("platform_identities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )
    contact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("customer_contacts.id", ondelete="CASCADE"),
        nullable=True,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
