"""Asset and AssetMovement models — customer material/tools stored at supplier."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import AssetMovementType
from app.models.mixins import TenantMixin, TimestampMixin

_MOVEMENT_TYPE_ENUM = Enum(
    AssetMovementType,
    name="asset_movement_type",
    native_enum=False,
    length=16,
    values_callable=lambda obj: [e.value for e in obj],
)


class Asset(Base, TimestampMixin, TenantMixin):
    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "customer_id", "code", name="uq_assets_tenant_id_customer_id_code"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)

    customer_id: Mapped[UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    unit: Mapped[str] = mapped_column(String(16), nullable=False, default="ks", server_default="ks")
    current_quantity: Mapped[Decimal] = mapped_column(
        Numeric(12, 3), nullable=False, default=0, server_default="0"
    )

    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Asset {self.code} {self.current_quantity} {self.unit}>"


class AssetMovement(Base, TimestampMixin, TenantMixin):
    __tablename__ = "asset_movements"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)

    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    type: Mapped[AssetMovementType] = mapped_column(_MOVEMENT_TYPE_ENUM, nullable=False)
    # Signed quantity: positive for receive, negative for issue/consume.
    # adjust can be either.
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)

    reference_order_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
