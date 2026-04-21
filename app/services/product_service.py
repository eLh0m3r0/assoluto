"""Product catalog service."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product import Product
from app.services import audit_service
from app.services.audit_service import SYSTEM_ACTOR, ActorInfo, diff_from_models


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
    stmt = select(Product).where(Product.is_active.is_(True))
    if query and query.strip():
        q = f"%{query.strip()}%"
        stmt = stmt.where(or_(Product.sku.ilike(q), Product.name.ilike(q)))
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
    audit_actor: ActorInfo | None = None,
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

    await audit_service.record(
        db,
        action="product.created",
        entity_type="product",
        entity_id=product.id,
        entity_label=f"{product.sku} — {product.name}",
        actor=audit_actor or SYSTEM_ACTOR,
        after={
            "sku": product.sku,
            "name": product.name,
            "default_price": (
                str(product.default_price) if product.default_price is not None else None
            ),
            "currency": product.currency,
        },
        tenant_id=tenant_id,
    )
    return product


async def get_product(db: AsyncSession, product_id: UUID) -> Product | None:
    return (await db.execute(select(Product).where(Product.id == product_id))).scalar_one_or_none()


async def update_product(
    db: AsyncSession,
    product: Product,
    *,
    sku: str,
    name: str,
    description: str | None,
    unit: str,
    default_price: Decimal | None,
    customer_id: UUID | None,
    audit_actor: ActorInfo | None = None,
) -> Product:
    sku = sku.strip()
    name = name.strip()
    if not sku or not name:
        raise ProductError("sku and name are required")

    # Re-check SKU uniqueness if the SKU or customer scope changed.
    if sku != product.sku or customer_id != product.customer_id:
        dup_stmt = select(Product).where(Product.sku == sku, Product.id != product.id)
        if customer_id is None:
            dup_stmt = dup_stmt.where(Product.customer_id.is_(None))
        else:
            dup_stmt = dup_stmt.where(Product.customer_id == customer_id)
        if (await db.execute(dup_stmt)).scalar_one_or_none() is not None:
            raise DuplicateProductSku(sku)

    # Snapshot before mutation so we can diff including the price fields
    # the plan calls out specifically.
    before_snapshot = type("_ProductSnapshot", (), {})()
    for field in ("sku", "name", "description", "unit", "default_price", "customer_id"):
        setattr(before_snapshot, field, getattr(product, field, None))

    product.sku = sku
    product.name = name
    product.description = description or None
    product.unit = unit or "ks"
    product.default_price = default_price
    product.customer_id = customer_id
    await db.flush()

    diff = diff_from_models(
        before_snapshot,
        product,
        ["sku", "name", "description", "unit", "default_price", "customer_id"],
    )
    if diff:
        await audit_service.record(
            db,
            action="product.updated",
            entity_type="product",
            entity_id=product.id,
            entity_label=f"{product.sku} — {product.name}",
            actor=audit_actor or SYSTEM_ACTOR,
            diff=diff,
            tenant_id=product.tenant_id,
        )
    return product


async def deactivate_product(db: AsyncSession, product: Product) -> None:
    """Soft-delete the product (is_active=False) — keeps it out of the
    catalog listing and the order-item search, but preserves the
    foreign-key relation from any historical order items."""
    product.is_active = False
    await db.flush()
