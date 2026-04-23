"""Customer (client company) domain services — create/list/lookup."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer, CustomerContact
from app.models.enums import OrderStatus
from app.models.order import Order
from app.services import audit_service
from app.services.audit_service import SYSTEM_ACTOR, ActorInfo, diff_from_models


async def list_customers(db: AsyncSession) -> list[Customer]:
    """Return all active customers in the current tenant, alphabetical."""
    result = await db.execute(
        select(Customer).where(Customer.is_active.is_(True)).order_by(Customer.name)
    )
    return list(result.scalars().all())


# Statuses that count as "open" for the engagement-metrics badge on
# the customer list. CLOSED / CANCELLED / DELIVERED don't count —
# those are terminal or near-terminal from the tenant's POV.
_OPEN_ORDER_STATUSES: tuple[OrderStatus, ...] = (
    OrderStatus.DRAFT,
    OrderStatus.SUBMITTED,
    OrderStatus.QUOTED,
    OrderStatus.CONFIRMED,
    OrderStatus.IN_PRODUCTION,
    OrderStatus.READY,
)


@dataclass(frozen=True)
class CustomerStats:
    """Aggregated counts shown on the customer list card."""

    customer: Customer
    contacts_active: int
    orders_open: int


async def list_customers_with_stats(db: AsyncSession) -> list[CustomerStats]:
    """Return every active customer with two counts attached.

    One round-trip: the two scalar subqueries in the SELECT let Postgres
    compute the aggregates without an N+1, and the index on
    ``customer_contacts.customer_id`` + ``orders.customer_id`` keeps it
    O(log n) per bucket. Honours RLS — no need for a separate tenant
    filter because the session's ``app.tenant_id`` already scopes each
    subquery.
    """
    contacts_subq = (
        select(func.count())
        .select_from(CustomerContact)
        .where(
            CustomerContact.customer_id == Customer.id,
            CustomerContact.is_active.is_(True),
        )
        .scalar_subquery()
    )
    orders_subq = (
        select(func.count())
        .select_from(Order)
        .where(
            Order.customer_id == Customer.id,
            Order.status.in_(_OPEN_ORDER_STATUSES),
        )
        .scalar_subquery()
    )
    stmt = (
        select(Customer, contacts_subq, orders_subq)
        .where(Customer.is_active.is_(True))
        .order_by(Customer.name)
    )
    rows = (await db.execute(stmt)).all()
    return [
        CustomerStats(customer=c, contacts_active=int(cc), orders_open=int(oo))
        for c, cc, oo in rows
    ]


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
    order_permissions: dict | None = None,
    audit_actor: ActorInfo | None = None,
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
        order_permissions=order_permissions or {},
    )
    db.add(customer)
    await db.flush()

    await audit_service.record(
        db,
        action="customer.created",
        entity_type="customer",
        entity_id=customer.id,
        entity_label=customer.name,
        actor=audit_actor or SYSTEM_ACTOR,
        after={
            "name": customer.name,
            "ico": customer.ico,
            "dic": customer.dic,
        },
        tenant_id=tenant_id,
    )
    return customer


async def update_customer(
    db: AsyncSession,
    customer: Customer,
    *,
    name: str,
    ico: str | None,
    dic: str | None,
    notes: str | None,
    order_permissions: dict | None = None,
    preferred_locale: str | None = None,
    audit_actor: ActorInfo | None = None,
) -> Customer:
    name = name.strip()
    if not name:
        raise ValueError("customer name is required")

    # Snapshot tracked fields BEFORE mutation so the diff picks up the
    # genuine prior state even after the attribute assignments below.
    before_snapshot = type("_CustomerSnapshot", (), {})()
    for field in ("name", "ico", "dic", "notes", "order_permissions", "preferred_locale"):
        setattr(before_snapshot, field, getattr(customer, field, None))

    customer.name = name
    customer.ico = ((ico or None) and ico.strip()) or None
    customer.dic = ((dic or None) and dic.strip()) or None
    customer.notes = ((notes or None) and notes.strip()) or None
    if order_permissions is not None:
        customer.order_permissions = order_permissions
    # ``None`` is a valid preferred_locale (NULL = inherit) so we always
    # apply what the caller passed; callers that don't want to touch
    # this field should omit the kwarg entirely.
    customer.preferred_locale = preferred_locale
    await db.flush()

    diff = diff_from_models(
        before_snapshot,
        customer,
        ["name", "ico", "dic", "notes", "order_permissions", "preferred_locale"],
    )
    if diff:
        await audit_service.record(
            db,
            action="customer.updated",
            entity_type="customer",
            entity_id=customer.id,
            entity_label=customer.name,
            actor=audit_actor or SYSTEM_ACTOR,
            diff=diff,
            tenant_id=customer.tenant_id,
        )
    return customer


async def delete_customer(
    db: AsyncSession,
    customer: Customer,
    *,
    audit_actor: ActorInfo | None = None,
) -> None:
    """Hard-delete a customer row (RESTRICT-protected by orders FK).

    The service stays in place for future router wiring; the audit row
    is written before the delete so the ``entity_label`` survives.
    """
    label = customer.name
    customer_id = customer.id
    tenant_id = customer.tenant_id

    await audit_service.record(
        db,
        action="customer.deleted",
        entity_type="customer",
        entity_id=customer_id,
        entity_label=label,
        actor=audit_actor or SYSTEM_ACTOR,
        before={
            "name": customer.name,
            "ico": customer.ico,
            "dic": customer.dic,
        },
        tenant_id=tenant_id,
    )
    await db.delete(customer)
    await db.flush()
