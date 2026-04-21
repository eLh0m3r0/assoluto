"""AuditEvent — append-only record of a domain action.

Backs §6 of the Sprint-3 plan. Services call
``app.services.audit_service.record(...)`` which writes a row of this
type in the caller's transaction; a downstream recent-activity feed
(§7) reads the same table.

Conventions:

- ``actor_type`` is one of ``"user"``, ``"contact"`` or ``"system"``.
  ``actor_id`` is the target principal's UUID or ``None`` for system
  actors. ``actor_label`` is cached so the row stays readable after
  the underlying row is deactivated or deleted.
- ``entity_type`` is a short lower-case string per domain (``order``,
  ``customer``, ``product``, ``user``). ``entity_id`` is the UUID of
  the affected row; ``entity_label`` is whatever makes sense to show
  in UI (order number, customer name, product SKU/name…).
- ``diff`` is an opaque JSON blob. The helper
  :func:`app.services.audit_service.diff_from_models` produces the
  ``{"before": {...}, "after": {...}}`` shape used by ``*.updated``
  events.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TenantMixin, TimestampMixin
from app.models.tenant import JsonColumn


class AuditEvent(Base, TimestampMixin, TenantMixin):
    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    # Polymorphic actor — see module docstring.
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    actor_label: Mapped[str] = mapped_column(String(255), nullable=False)

    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    entity_label: Mapped[str] = mapped_column(String(255), nullable=False)

    diff: Mapped[dict[str, Any] | None] = mapped_column(JsonColumn, nullable=True)

    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AuditEvent {self.action} {self.entity_type}:{self.entity_label}>"
