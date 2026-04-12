"""Customer (client company) domain services — create/list/lookup."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer, CustomerContact


async def list_customers(db: AsyncSession) -> list[Customer]:
    """Return all active customers in the current tenant, alphabetical."""
    result = await db.execute(
        select(Customer).where(Customer.is_active.is_(True)).order_by(Customer.name)
    )
    return list(result.scalars().all())


async def get_customer(db: AsyncSession, customer_id: UUID) -> Customer | None:
    return (
        await db.execute(select(Customer).where(Customer.id == customer_id))
    ).scalar_one_or_none()


async def list_contacts_for_customer(db: AsyncSession, customer_id: UUID) -> list[CustomerContact]:
    result = await db.execute(
        select(CustomerContact)
        .where(CustomerContact.customer_id == customer_id)
        .order_by(CustomerContact.full_name)
    )
    return list(result.scalars().all())


async def create_customer(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    name: str,
    ico: str | None = None,
    dic: str | None = None,
    notes: str | None = None,
) -> Customer:
    name = name.strip()
    if not name:
        raise ValueError("customer name is required")

    customer = Customer(
        tenant_id=tenant_id,
        name=name,
        ico=((ico or None) and ico.strip()) or None,
        dic=((dic or None) and dic.strip()) or None,
        notes=((notes or None) and notes.strip()) or None,
    )
    db.add(customer)
    await db.flush()
    return customer
