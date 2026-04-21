"""Order, OrderItem, OrderStatusHistory, OrderComment models.

Core of the MVP domain. Status transitions live in
`app.services.order_service`; this module is pure data definition.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import OrderStatus
from app.models.mixins import TenantMixin, TimestampMixin

_ORDER_STATUS_ENUM = Enum(
    OrderStatus,
    name="order_status",
    native_enum=False,
    length=32,
    values_callable=lambda obj: [e.value for e in obj],
)


class Order(Base, TimestampMixin, TenantMixin):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("tenant_id", "number", name="uq_orders_tenant_id_number"),
        Index("ix_orders_tenant_id_status", "tenant_id", "status"),
        Index(
            "ix_orders_tenant_id_customer_id_created_at",
            "tenant_id",
            "customer_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)

    customer_id: Mapped[UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Human-facing serial per tenant, e.g. "2026-000001".
    number: Mapped[str] = mapped_column(String(32), nullable=False)

    title: Mapped[str] = mapped_column(String(255), nullable=False)

    status: Mapped[OrderStatus] = mapped_column(
        _ORDER_STATUS_ENUM,
        nullable=False,
        default=OrderStatus.DRAFT,
        server_default=OrderStatus.DRAFT.value,
    )

    created_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_by_contact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("customer_contacts.id", ondelete="SET NULL"), nullable=True
    )

    requested_delivery_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    promised_delivery_at: Mapped[date | None] = mapped_column(Date, nullable=True)

    quoted_total: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[str] = mapped_column(
        String(8), nullable=False, default="CZK", server_default="CZK"
    )

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Recorded on transition into ``OrderStatus.DELIVERED``; drives SLA
    # on-time calculations in ``app.services.sla_service``. Nullable —
    # historical or never-delivered orders stay NULL.
    delivered_at: Mapped[date | None] = mapped_column(Date, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Order {self.number} status={self.status.value}>"


class OrderItem(Base, TimestampMixin, TenantMixin):
    __tablename__ = "order_items"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)

    order_id: Mapped[UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Optional link to a catalog product (set in M4). NULL = free-text item.
    product_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)

    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    description: Mapped[str] = mapped_column(String(2000), nullable=False)

    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False, default="ks", server_default="ks")

    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    line_total: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<OrderItem #{self.position} qty={self.quantity}>"


class OrderStatusHistory(Base, TimestampMixin, TenantMixin):
    __tablename__ = "order_status_history"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)

    order_id: Mapped[UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    from_status: Mapped[OrderStatus | None] = mapped_column(_ORDER_STATUS_ENUM, nullable=True)
    to_status: Mapped[OrderStatus] = mapped_column(_ORDER_STATUS_ENUM, nullable=False)

    changed_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    changed_by_contact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("customer_contacts.id", ondelete="SET NULL"), nullable=True
    )

    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class OrderComment(Base, TimestampMixin, TenantMixin):
    __tablename__ = "order_comments"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)

    order_id: Mapped[UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    author_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    author_contact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("customer_contacts.id", ondelete="SET NULL"), nullable=True
    )

    body: Mapped[str] = mapped_column(Text, nullable=False)

    # Internal comments are visible only to tenant staff.
    is_internal: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
