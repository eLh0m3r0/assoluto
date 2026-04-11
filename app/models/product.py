"""Product (catalog item) model.

A Product can be tenant-wide (`customer_id IS NULL`) or scoped to a
specific customer. Staff list + autocomplete filters by customer
accordingly.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import Boolean, ForeignKey, Numeric, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TenantMixin, TimestampMixin


class Product(Base, TimestampMixin, TenantMixin):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "customer_id",
            "sku",
            name="uq_products_tenant_id_customer_id_sku",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)

    # NULL = shared per-tenant catalog
    customer_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    sku: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    unit: Mapped[str] = mapped_column(String(16), nullable=False, default="ks", server_default="ks")
    default_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[str] = mapped_column(
        String(8), nullable=False, default="CZK", server_default="CZK"
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Product {self.sku} {self.name!r}>"
