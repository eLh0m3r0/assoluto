"""Tenant model — top-level owner of all data in the portal.

A tenant is a supplier company (e.g. 4MEX) that runs the portal for its
own customers. `Tenant` is the ONLY table without a `tenant_id` column and
without Row-Level Security; it is the anchor of the multi-tenant hierarchy.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, Integer, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin

# Cross-DB JSON column: JSONB on PostgreSQL, plain JSON on SQLite/others.
# Use this for any settings/metadata columns so SQLite-based unit tests
# can exercise the ORM without losing type fidelity in production.
JsonColumn = JSON().with_variant(JSONB, "postgresql")


class Tenant(Base, TimestampMixin):
    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    billing_email: Mapped[str] = mapped_column(String(320), nullable=False)

    # S3 key prefix used for this tenant's uploaded files.
    storage_prefix: Mapped[str] = mapped_column(String(255), nullable=False)

    # Per-tenant order number sequence. Incremented under FOR UPDATE when
    # creating a new order.
    next_order_seq: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # Free-form per-tenant settings (branding, feature flags, etc.).
    settings: Mapped[dict[str, Any]] = mapped_column(
        JsonColumn, nullable=False, default=dict, server_default="{}"
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Tenant id={self.id} slug={self.slug!r}>"
