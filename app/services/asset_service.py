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


class ForeignOrderReference(AssetError):
    """The referenced order does not exist or belongs to another customer."""


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
    audit_actor=None,
) -> AssetMovement:
    """Insert a movement and recompute the asset's current_quantity.

    The asset row is re-loaded ``FOR UPDATE`` inside this function so
    concurrent movements on the same asset are serialised at the DB
    level — otherwise two simultaneous POSTs could both read the same
    ``current_quantity``, both pass the stock check, and the last
    writer would persist a wrong total. Round-4 audit A2 fix.
    """
    if quantity is None:
        raise AssetError("quantity is required")

    qty = Decimal(quantity)
    if type_ != AssetMovementType.ADJUST and qty <= 0:
        raise AssetError("quantity must be positive (use ADJUST for corrections)")

    if not qty.is_finite():
        raise AssetError("quantity must be a finite number")

    signed = _signed_quantity(type_, qty)

    # LOGIC-24: the movement must reference an order of the asset's own
    # customer. The supplier is liable for the customer's material; a
    # consume booked against another customer's order cannot be traced
    # back, and a made-up id used to hit the FK and 500.
    if reference_order_id is not None:
        from app.models.order import Order

        ref_customer = (
            await db.execute(select(Order.customer_id).where(Order.id == reference_order_id))
        ).scalar_one_or_none()
        if ref_customer is None or ref_customer != asset.customer_id:
            raise ForeignOrderReference("reference order belongs to another customer")

    # Re-read the row under a row-level lock so the current_quantity
    # is guaranteed not to change between our read and write.
    locked_asset = (
        await db.execute(select(Asset).where(Asset.id == asset.id).with_for_update())
    ).scalar_one()

    new_total = (locked_asset.current_quantity or Decimal("0")) + signed
    # ADJUST is a correction, not a licence to book stock the customer
    # never handed over: it may not take the balance below zero either.
    if new_total < 0:
        raise InsufficientStock(
            f"not enough stock: {locked_asset.current_quantity} < {qty.copy_abs()}"
        )

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
    before_total = locked_asset.current_quantity
    locked_asset.current_quantity = new_total
    await db.flush()

    from app.services import audit_service
    from app.services.audit_service import SYSTEM_ACTOR

    await audit_service.record(
        db,
        action="asset.movement_added",
        entity_type="asset",
        entity_id=locked_asset.id,
        entity_label=getattr(locked_asset, "name", None) or str(locked_asset.id),
        actor=audit_actor or SYSTEM_ACTOR,
        before={"current_quantity": str(before_total)},
        after={
            "current_quantity": str(new_total),
            "type": type_.value,
            "quantity": str(signed),
            "reference_order_id": str(reference_order_id) if reference_order_id else None,
            "note": note or None,
        },
        tenant_id=tenant_id,
    )
    return movement
