"""Order domain service: creation, transitions, items, comments.

All the business rules live here so the HTTP layer stays thin. The
state-machine table at the top is the single source of truth for what
transitions are allowed and which actor may trigger them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer
from app.models.enums import OrderStatus
from app.models.order import Order, OrderComment, OrderItem, OrderStatusHistory
from app.models.tenant import Tenant


class OrderError(Exception):
    """Base class for order domain errors."""


class ForbiddenTransition(OrderError):
    pass


class ForbiddenActor(OrderError):
    pass


class OrderNotFound(OrderError):
    pass


class OrderAccessDenied(OrderError):
    pass


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

# Allowed forward transitions. Keys are the current status, values the set
# of statuses the order may move to next.
ALLOWED_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.DRAFT: {OrderStatus.SUBMITTED, OrderStatus.CANCELLED},
    OrderStatus.SUBMITTED: {
        OrderStatus.QUOTED,
        OrderStatus.CONFIRMED,
        OrderStatus.CANCELLED,
    },
    OrderStatus.QUOTED: {OrderStatus.CONFIRMED, OrderStatus.CANCELLED},
    OrderStatus.CONFIRMED: {OrderStatus.IN_PRODUCTION, OrderStatus.CANCELLED},
    OrderStatus.IN_PRODUCTION: {OrderStatus.READY, OrderStatus.CANCELLED},
    OrderStatus.READY: {OrderStatus.DELIVERED},
    OrderStatus.DELIVERED: {OrderStatus.CLOSED},
    OrderStatus.CLOSED: set(),
    OrderStatus.CANCELLED: set(),
}

# Which actor type ("user" = tenant staff, "contact" = customer contact)
# can perform a given transition. A transition not listed here is
# administratively blocked regardless of the ALLOWED_TRANSITIONS table.
ACTOR_RULES: dict[tuple[OrderStatus, OrderStatus], set[str]] = {
    (OrderStatus.DRAFT, OrderStatus.SUBMITTED): {"contact", "user"},
    (OrderStatus.DRAFT, OrderStatus.CANCELLED): {"contact", "user"},
    (OrderStatus.SUBMITTED, OrderStatus.QUOTED): {"user"},
    (OrderStatus.SUBMITTED, OrderStatus.CONFIRMED): {"user"},
    (OrderStatus.SUBMITTED, OrderStatus.CANCELLED): {"contact", "user"},
    (OrderStatus.QUOTED, OrderStatus.CONFIRMED): {"contact", "user"},
    (OrderStatus.QUOTED, OrderStatus.CANCELLED): {"contact", "user"},
    (OrderStatus.CONFIRMED, OrderStatus.IN_PRODUCTION): {"user"},
    (OrderStatus.CONFIRMED, OrderStatus.CANCELLED): {"user"},
    (OrderStatus.IN_PRODUCTION, OrderStatus.READY): {"user"},
    (OrderStatus.IN_PRODUCTION, OrderStatus.CANCELLED): {"user"},
    (OrderStatus.READY, OrderStatus.DELIVERED): {"user"},
    (OrderStatus.DELIVERED, OrderStatus.CLOSED): {"user"},
}


@dataclass(frozen=True)
class ActorRef:
    """A compact reference to the entity performing an action on an order."""

    type: str  # "user" | "contact"
    id: UUID
    customer_id: UUID | None = None  # set for contacts


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


async def list_orders_for_principal(
    db: AsyncSession,
    *,
    actor: ActorRef,
    status_filter: OrderStatus | None = None,
) -> list[Order]:
    """Return orders visible to the actor, newest first.

    Tenant isolation comes from RLS (the session is already scoped). On
    top of that, customer contacts see only their own customer's orders.
    """
    stmt = select(Order).order_by(Order.created_at.desc())
    if actor.type == "contact":
        stmt = stmt.where(Order.customer_id == actor.customer_id)
    if status_filter is not None:
        stmt = stmt.where(Order.status == status_filter)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_order_for_principal(db: AsyncSession, *, order_id: UUID, actor: ActorRef) -> Order:
    """Load an order enforcing customer-scoped access for contacts."""
    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if order is None:
        raise OrderNotFound()
    if actor.type == "contact" and order.customer_id != actor.customer_id:
        raise OrderAccessDenied()
    return order


async def list_items(db: AsyncSession, order_id: UUID) -> list[OrderItem]:
    result = await db.execute(
        select(OrderItem)
        .where(OrderItem.order_id == order_id)
        .order_by(OrderItem.position, OrderItem.created_at)
    )
    return list(result.scalars().all())


async def list_comments(
    db: AsyncSession, *, order_id: UUID, include_internal: bool
) -> list[OrderComment]:
    stmt = (
        select(OrderComment)
        .where(OrderComment.order_id == order_id)
        .order_by(OrderComment.created_at)
    )
    if not include_internal:
        stmt = stmt.where(OrderComment.is_internal.is_(False))
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def list_status_history(db: AsyncSession, order_id: UUID) -> list[OrderStatusHistory]:
    result = await db.execute(
        select(OrderStatusHistory)
        .where(OrderStatusHistory.order_id == order_id)
        .order_by(OrderStatusHistory.created_at)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


async def _next_order_number(db: AsyncSession, *, tenant_id: UUID) -> str:
    """Atomically allocate the next per-tenant order number.

    Uses a row-level lock on the tenant row so concurrent creations can't
    collide. The returned value is "<year>-<seq:06>".
    """
    # SELECT ... FOR UPDATE on the tenant row.
    tenant = (
        await db.execute(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    ).scalar_one()
    next_seq = (tenant.next_order_seq or 0) + 1
    tenant.next_order_seq = next_seq
    # Flush the update so the lock holds through the rest of the
    # enclosing transaction.
    await db.flush()

    now = datetime.now(UTC)
    return f"{now.year}-{next_seq:06d}"


async def create_order(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    actor: ActorRef,
    customer_id: UUID,
    title: str,
    requested_delivery_at=None,
    notes: str | None = None,
) -> Order:
    """Create a new order in DRAFT state.

    Contacts may only create orders for their own customer.
    """
    title = title.strip()
    if not title:
        raise OrderError("title is required")

    if actor.type == "contact":
        if actor.customer_id != customer_id:
            raise OrderAccessDenied()
    else:
        # Staff: make sure the customer exists in this tenant.
        customer = (
            await db.execute(select(Customer).where(Customer.id == customer_id))
        ).scalar_one_or_none()
        if customer is None:
            raise OrderError("unknown customer")

    number = await _next_order_number(db, tenant_id=tenant_id)

    order = Order(
        tenant_id=tenant_id,
        customer_id=customer_id,
        number=number,
        title=title,
        status=OrderStatus.DRAFT,
        requested_delivery_at=requested_delivery_at,
        notes=notes or None,
        created_by_user_id=actor.id if actor.type == "user" else None,
        created_by_contact_id=actor.id if actor.type == "contact" else None,
    )
    db.add(order)
    await db.flush()

    db.add(
        OrderStatusHistory(
            tenant_id=tenant_id,
            order_id=order.id,
            from_status=None,
            to_status=OrderStatus.DRAFT,
            changed_by_user_id=actor.id if actor.type == "user" else None,
            changed_by_contact_id=actor.id if actor.type == "contact" else None,
        )
    )
    await db.flush()
    return order


def _recalculate_line_total(item: OrderItem) -> None:
    if item.unit_price is None:
        item.line_total = None
    else:
        item.line_total = (Decimal(item.quantity) * Decimal(item.unit_price)).quantize(
            Decimal("0.01")
        )


async def add_item(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    order: Order,
    actor: ActorRef,
    description: str,
    quantity: Decimal,
    unit: str = "ks",
    unit_price: Decimal | None = None,
    product_id: UUID | None = None,
    notes: str | None = None,
) -> OrderItem:
    """Append a line item to an order.

    - Only DRAFT orders accept item changes from contacts.
    - Staff can edit items while the order is in DRAFT, SUBMITTED or
      QUOTED (needed to add quoted prices).
    """
    _ensure_item_editable(order, actor)

    description = description.strip()
    if not description:
        raise OrderError("item description is required")
    if quantity is None or Decimal(quantity) <= 0:
        raise OrderError("quantity must be positive")

    # Pick the next position by MAX(position) + 1 to be insertion-order
    # stable without relying on created_at timestamps.
    max_pos = (
        await db.execute(
            select(func.coalesce(func.max(OrderItem.position), -1)).where(
                OrderItem.order_id == order.id
            )
        )
    ).scalar()

    item = OrderItem(
        tenant_id=tenant_id,
        order_id=order.id,
        product_id=product_id,
        position=int(max_pos or -1) + 1,
        description=description,
        quantity=Decimal(quantity),
        unit=unit or "ks",
        unit_price=Decimal(unit_price) if unit_price is not None else None,
        notes=notes or None,
    )
    _recalculate_line_total(item)
    db.add(item)
    await db.flush()
    return item


async def update_item_pricing(
    db: AsyncSession,
    *,
    order: Order,
    item: OrderItem,
    actor: ActorRef,
    unit_price: Decimal | None,
) -> OrderItem:
    """Set or clear an item's unit_price (staff only, while pricing)."""
    if actor.type != "user":
        raise ForbiddenActor("pricing is a staff action")
    if order.status not in (OrderStatus.SUBMITTED, OrderStatus.QUOTED, OrderStatus.DRAFT):
        raise ForbiddenTransition(f"cannot price an order in status {order.status}")
    item.unit_price = Decimal(unit_price) if unit_price is not None else None
    _recalculate_line_total(item)
    await db.flush()
    return item


async def remove_item(db: AsyncSession, *, order: Order, item: OrderItem, actor: ActorRef) -> None:
    _ensure_item_editable(order, actor)
    await db.delete(item)
    await db.flush()


def _ensure_item_editable(order: Order, actor: ActorRef) -> None:
    if actor.type == "contact":
        if order.status != OrderStatus.DRAFT:
            raise ForbiddenTransition("customer contacts may only edit items in DRAFT")
        return
    # Staff: allow item edits up to (and including) QUOTED.
    if order.status not in (
        OrderStatus.DRAFT,
        OrderStatus.SUBMITTED,
        OrderStatus.QUOTED,
    ):
        raise ForbiddenTransition(f"items are locked once the order is in {order.status}")


async def _recompute_quoted_total(db: AsyncSession, order: Order) -> None:
    total = (
        await db.execute(
            select(func.coalesce(func.sum(OrderItem.line_total), 0)).where(
                OrderItem.order_id == order.id
            )
        )
    ).scalar()
    order.quoted_total = Decimal(total) if total is not None else Decimal("0")
    await db.flush()


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


async def transition_order(
    db: AsyncSession,
    *,
    order: Order,
    to_status: OrderStatus,
    actor: ActorRef,
    note: str | None = None,
) -> Order:
    """Advance the order to `to_status` after validating the move."""
    allowed = ALLOWED_TRANSITIONS.get(order.status, set())
    if to_status not in allowed:
        raise ForbiddenTransition(
            f"cannot transition from {order.status.value} to {to_status.value}"
        )

    allowed_actors = ACTOR_RULES.get((order.status, to_status), set())
    if actor.type not in allowed_actors:
        raise ForbiddenActor(
            f"{actor.type} cannot perform {order.status.value} -> {to_status.value}"
        )

    if actor.type == "contact" and order.customer_id != actor.customer_id:
        raise OrderAccessDenied()

    # Side effects for specific transitions.
    now = datetime.now(UTC)
    previous = order.status
    order.status = to_status

    if to_status == OrderStatus.SUBMITTED:
        order.submitted_at = now
    if to_status == OrderStatus.QUOTED:
        # Make sure we have a total computed from the item prices.
        await _recompute_quoted_total(db, order)
    if to_status == OrderStatus.CLOSED:
        order.closed_at = now
    if to_status == OrderStatus.CANCELLED:
        order.cancelled_at = now

    db.add(
        OrderStatusHistory(
            tenant_id=order.tenant_id,
            order_id=order.id,
            from_status=previous,
            to_status=to_status,
            changed_by_user_id=actor.id if actor.type == "user" else None,
            changed_by_contact_id=actor.id if actor.type == "contact" else None,
            note=note,
        )
    )
    await db.flush()
    return order


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


async def add_comment(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    order: Order,
    actor: ActorRef,
    body: str,
    is_internal: bool = False,
) -> OrderComment:
    body = body.strip()
    if not body:
        raise OrderError("comment body is required")
    if is_internal and actor.type != "user":
        raise ForbiddenActor("internal comments are staff-only")
    if actor.type == "contact" and order.customer_id != actor.customer_id:
        raise OrderAccessDenied()

    comment = OrderComment(
        tenant_id=tenant_id,
        order_id=order.id,
        body=body,
        is_internal=is_internal,
        author_user_id=actor.id if actor.type == "user" else None,
        author_contact_id=actor.id if actor.type == "contact" else None,
    )
    db.add(comment)
    await db.flush()
    return comment


# Silence "text imported but unused" from older scaffolding; the helper is
# kept available for migration-style SQL if a caller needs it later.
_ = text
