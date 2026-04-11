"""Asset service — tracks customer-owned material stored at the supplier.

Movements are stored with SIGNED quantities:
    receive:  +qty
    issue:    -qty
    consume:  -qty
    adjust:   any sign

The asset's `current_quantity` is recomputed inside the same transaction
as every movement insertion so listing never needs to reduce the full
history.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset, AssetMovement
from app.models.customer import Customer
from app.models.enums import AssetMovementType


class AssetError(Exception):
    pass


class InsufficientStock(AssetError):
    pass


async def list_assets(db: AsyncSession, *, customer_id: UUID | None = None) -> list[Asset]:
    """List active assets. Pass `customer_id` to scope for a client view."""
    stmt = select(Asset).where(Asset.is_active.is_(True)).order_by(Asset.code)
    if customer_id is not None:
        stmt = stmt.where(Asset.customer_id == customer_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_asset(db: AsyncSession, asset_id: UUID) -> Asset | None:
    return (await db.execute(select(Asset).where(Asset.id == asset_id))).scalar_one_or_none()


async def list_movements(db: AsyncSession, *, asset_id: UUID) -> list[AssetMovement]:
    result = await db.execute(
        select(AssetMovement)
        .where(AssetMovement.asset_id == asset_id)
        .order_by(AssetMovement.occurred_at.desc(), AssetMovement.created_at.desc())
    )
    return list(result.scalars().all())


async def create_asset(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    customer_id: UUID,
    code: str,
    name: str,
    unit: str = "ks",
    description: str | None = None,
    location: str | None = None,
) -> Asset:
    code = code.strip()
    name = name.strip()
    if not code or not name:
        raise AssetError("code and name are required")

    customer = (
        await db.execute(select(Customer).where(Customer.id == customer_id))
    ).scalar_one_or_none()
    if customer is None:
        raise AssetError("unknown customer")

    asset = Asset(
        tenant_id=tenant_id,
        customer_id=customer_id,
        code=code,
        name=name,
        unit=unit or "ks",
        description=description or None,
        location=location or None,
        current_quantity=Decimal("0"),
    )
    db.add(asset)
    await db.flush()
    return asset


def _signed_quantity(type_: AssetMovementType, raw: Decimal) -> Decimal:
    """Return the value to store on the movement row given user input.

    User inputs a positive magnitude for all types except `adjust`, which
    accepts its own sign.
    """
    mag = raw if type_ == AssetMovementType.ADJUST else raw.copy_abs()
    if type_ == AssetMovementType.RECEIVE:
        return mag
    if type_ in (AssetMovementType.ISSUE, AssetMovementType.CONSUME):
        return -mag
    return mag  # adjust: as given


async def add_movement(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    asset: Asset,
    type_: AssetMovementType,
    quantity: Decimal,
    note: str | None = None,
    reference_order_id: UUID | None = None,
    created_by_user_id: UUID | None = None,
    occurred_at: datetime | None = None,
) -> AssetMovement:
    """Insert a movement and recompute the asset's current_quantity."""
    if quantity is None:
        raise AssetError("quantity is required")

    qty = Decimal(quantity)
    if type_ != AssetMovementType.ADJUST and qty <= 0:
        raise AssetError("quantity must be positive (use ADJUST for corrections)")

    signed = _signed_quantity(type_, qty)

    new_total = (asset.current_quantity or Decimal("0")) + signed
    if type_ in (AssetMovementType.ISSUE, AssetMovementType.CONSUME) and new_total < 0:
        raise InsufficientStock(f"not enough stock: {asset.current_quantity} < {qty.copy_abs()}")

    movement = AssetMovement(
        tenant_id=tenant_id,
        asset_id=asset.id,
        type=type_,
        quantity=signed,
        reference_order_id=reference_order_id,
        occurred_at=occurred_at or datetime.now(UTC),
        note=note or None,
        created_by_user_id=created_by_user_id,
    )
    db.add(movement)
    asset.current_quantity = new_total
    await db.flush()
    return movement
