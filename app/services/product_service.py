"""Product catalog service."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product import Product


class ProductError(Exception):
    pass


class DuplicateProductSku(ProductError):
    def __init__(self, sku: str) -> None:
        super().__init__(f"Product with SKU {sku!r} already exists")
        self.sku = sku


async def list_products(db: AsyncSession) -> list[Product]:
    """Return all active products (both shared and customer-scoped)."""
    result = await db.execute(
        select(Product).where(Product.is_active.is_(True)).order_by(Product.name)
    )
    return list(result.scalars().all())


async def search_products(
    db: AsyncSession,
    *,
    query: str,
    customer_id: UUID | None = None,
    limit: int = 20,
) -> list[Product]:
    """Search the catalog for use in an order-item autocomplete.

    When `customer_id` is given, returns shared products AND products
    dedicated to that customer. With no scope, returns everything.
    Matches `sku` or `name` case-insensitively.
    """
    q = f"%{query.strip()}%"
    stmt = (
        select(Product)
        .where(Product.is_active.is_(True))
        .where(or_(Product.sku.ilike(q), Product.name.ilike(q)))
    )
    if customer_id is not None:
        stmt = stmt.where(or_(Product.customer_id.is_(None), Product.customer_id == customer_id))
    stmt = stmt.order_by(Product.name).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def create_product(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    sku: str,
    name: str,
    description: str | None = None,
    unit: str = "ks",
    default_price: Decimal | None = None,
    currency: str = "CZK",
    customer_id: UUID | None = None,
) -> Product:
    sku = sku.strip()
    name = name.strip()
    if not sku or not name:
        raise ProductError("sku and name are required")

    # Enforce SKU uniqueness at the app layer because the Postgres
    # UNIQUE(tenant_id, customer_id, sku) constraint treats two NULL
    # customer_ids as distinct values (SQL NULL semantics), so two
    # shared products with the same SKU would slip through otherwise.
    dup_stmt = select(Product).where(Product.sku == sku)
    if customer_id is None:
        dup_stmt = dup_stmt.where(Product.customer_id.is_(None))
    else:
        dup_stmt = dup_stmt.where(Product.customer_id == customer_id)
    existing = (await db.execute(dup_stmt)).scalar_one_or_none()
    if existing is not None:
        raise DuplicateProductSku(sku)

    product = Product(
        tenant_id=tenant_id,
        customer_id=customer_id,
        sku=sku,
        name=name,
        description=(description or None),
        unit=unit or "ks",
        default_price=Decimal(default_price) if default_price is not None else None,
        currency=currency or "CZK",
    )
    db.add(product)
    await db.flush()
    return product
