"""Order domain service: creation, transitions, items, comments.

All the business rules live here so the HTTP layer stays thin. The
state-machine table at the top is the single source of truth for what
transitions are allowed and which actor may trigger them.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Select, func, select
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

# Allowed forward transitions for CUSTOMER CONTACTS. Staff (tenant users)
# can set any status at any time — the state machine only constrains
# the customer-facing side.
CONTACT_ALLOWED_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.DRAFT: {OrderStatus.SUBMITTED, OrderStatus.CANCELLED},
    OrderStatus.SUBMITTED: {OrderStatus.CANCELLED},
    OrderStatus.QUOTED: {OrderStatus.CONFIRMED, OrderStatus.CANCELLED},
    OrderStatus.CONFIRMED: set(),
    OrderStatus.IN_PRODUCTION: set(),
    OrderStatus.READY: set(),
    OrderStatus.DELIVERED: set(),
    OrderStatus.CLOSED: set(),
    OrderStatus.CANCELLED: set(),
}

# All statuses — used by staff who can move an order to any status.
ALL_STATUSES: set[OrderStatus] = set(OrderStatus)


@dataclass(frozen=True)
class ActorRef:
    """A compact reference to the entity performing an action on an order."""

    type: str  # "user" | "contact"
    id: UUID
    customer_id: UUID | None = None  # set for contacts


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def build_orders_query(
    *,
    actor: ActorRef,
    status: OrderStatus | None = None,
    customer_id: UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    q: str | None = None,
) -> Select:
    """Build the base `SELECT orders` query shared by list + CSV export.

    Tenant isolation comes from RLS on the session. On top of that,
    customer contacts are constrained to their own customer's orders;
    the ``customer_id`` filter is applied only when the actor is staff.

    ``date_from`` / ``date_to`` are **inclusive** bounds compared against
    ``Order.created_at`` (truncated to a calendar date on the caller side
    by passing a ``date`` value). A ``None`` bound means "unbounded".
    Returns the base ``Select``; callers add ``.limit()`` / ``.offset()``.
    """
    stmt = select(Order).order_by(Order.created_at.desc())
    if actor.type == "contact":
        stmt = stmt.where(Order.customer_id == actor.customer_id)
    elif customer_id is not None:
        stmt = stmt.where(Order.customer_id == customer_id)
    if status is not None:
        stmt = stmt.where(Order.status == status)
    if date_from is not None:
        stmt = stmt.where(Order.created_at >= date_from)
    if date_to is not None:
        # Inclusive upper bound — match anything strictly before the
        # start of the next day so full-day ranges behave as expected.
        from datetime import timedelta

        stmt = stmt.where(Order.created_at < date_to + timedelta(days=1))
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where((Order.number.ilike(pattern)) | (Order.title.ilike(pattern)))
    return stmt


async def list_orders_for_principal(
    db: AsyncSession,
    *,
    actor: ActorRef,
    status_filter: OrderStatus | None = None,
    customer_filter: UUID | None = None,
    search: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[Order], int]:
    """Return (orders, total_count) visible to the actor, newest first.

    Tenant isolation comes from RLS (the session is already scoped). On
    top of that, customer contacts see only their own customer's orders.
    """
    stmt = build_orders_query(
        actor=actor,
        status=status_filter,
        customer_id=customer_filter,
        q=search,
    )
    # Count(*) over the same filter set — re-run build_orders_query as a
    # subquery so the WHERE clauses stay in sync automatically.
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())

    total = int((await db.execute(count_stmt)).scalar() or 0)
    stmt = stmt.offset(max(0, offset)).limit(max(1, min(limit, 100)))
    result = await db.execute(stmt)
    return list(result.scalars().all()), total


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

    Instead of relying on a counter field (which can drift after manual
    inserts, seed scripts, or data imports), we derive the next sequence
    from the actual MAX(number) in the orders table for this tenant/year.
    A FOR UPDATE lock on the tenant row serialises concurrent creations.
    """
    now = datetime.now(UTC)
    year = now.year
    prefix = f"{year}-"

    # Lock the tenant row to serialise concurrent order creation.
    await db.execute(select(Tenant).where(Tenant.id == tenant_id).with_for_update())

    # Find the highest existing number for this year.
    max_number = (
        await db.execute(
            select(func.max(Order.number)).where(
                Order.tenant_id == tenant_id,
                Order.number.like(f"{prefix}%"),
            )
        )
    ).scalar()

    if max_number is not None:
        try:
            current_seq = int(max_number.split("-", 1)[1])
        except (ValueError, IndexError):
            current_seq = 0
    else:
        current_seq = 0

    next_seq = current_seq + 1
    return f"{year}-{next_seq:06d}"


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


async def remove_item(db: AsyncSession, *, order: Order, item: OrderItem, actor: ActorRef) -> None:
    _ensure_item_editable(order, actor)
    await db.delete(item)
    await db.flush()
    # Recompute the cached quoted total — the deleted line no longer
    # contributes. Keeps the dashboard card in sync with the items list.
    await _recompute_quoted_total(db, order)


async def update_item(
    db: AsyncSession,
    *,
    order: Order,
    item: OrderItem,
    quantity: Decimal | None = None,
    unit_price: Decimal | None = None,
    note: str | None = None,
    actor: ActorRef | None = None,
) -> OrderItem:
    """Partially update a line item on a DRAFT order.

    Only provided (non-``None``) fields are applied — this mirrors the
    field-level autosave UX where a single input change PATCHes just that
    field. The order must be in ``DRAFT`` for **any** actor (staff or
    contact); once an order has moved on, edits go through the full
    transition flow instead.

    The ``actor`` argument is accepted for future audit-hook compatibility
    (see §6 in the roadmap); this revision does not record audit events
    so the parameter is currently ignored if no audit service is wired.
    """
    if order.status != OrderStatus.DRAFT:
        raise ForbiddenTransition("items can only be edited while the order is a draft")

    # Contact scope check — cannot patch somebody else's customer's order.
    if actor is not None and actor.type == "contact" and order.customer_id != actor.customer_id:
        raise OrderAccessDenied()

    if quantity is not None:
        if Decimal(quantity) <= 0:
            raise OrderError("quantity must be positive")
        item.quantity = Decimal(quantity)

    if unit_price is not None:
        # A sentinel Decimal("-1") is NOT used — callers pass None to
        # signal "do not touch". Passing a real negative price is an error.
        if Decimal(unit_price) < 0:
            raise OrderError("unit_price must not be negative")
        item.unit_price = Decimal(unit_price)

    if note is not None:
        # Empty string clears the note; treat "   " as empty too.
        cleaned = note.strip() or None
        item.notes = cleaned

    _recalculate_line_total(item)
    await db.flush()
    # Keep ``quoted_total`` aligned with the sum of line totals so the
    # header card on the detail page reflects the just-saved change.
    await _recompute_quoted_total(db, order)
    return item


def _ensure_item_editable(order: Order, actor: ActorRef) -> None:
    """Check whether the actor can add/remove/edit items on the order.

    Staff have full edit access at all times. Customer contacts can
    only modify items while the order is in DRAFT.
    """
    if actor.type == "contact":
        if order.status != OrderStatus.DRAFT:
            raise ForbiddenTransition("customer contacts may only edit items in DRAFT")
        return
    # Staff: always allowed — full administrative control.


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
    """Advance the order to `to_status` after validating the move.

    **Staff (tenant users) can set any status at any time** — they have
    full administrative control over the order lifecycle. The state
    machine only constrains customer contacts.
    """
    if order.status == to_status:
        raise ForbiddenTransition("already in that status")

    if actor.type == "contact":
        # Contacts are constrained by the state machine.
        allowed = CONTACT_ALLOWED_TRANSITIONS.get(order.status, set())
        if to_status not in allowed:
            raise ForbiddenTransition(
                f"cannot transition from {order.status.value} to {to_status.value}"
            )
        if order.customer_id != actor.customer_id:
            raise OrderAccessDenied()
    # Staff: no transition restrictions — they can move to any status.

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


@dataclass
class BulkResult:
    """Outcome of a bulk status transition.

    ``succeeded`` lists order IDs that transitioned cleanly; ``errors``
    maps order ID → human-readable reason for the ones that did not.
    The caller is responsible for committing the session after inspecting
    the result — the service only flushes so failures can short-circuit
    without poisoning the outer transaction.
    """

    succeeded: list[UUID] = field(default_factory=list)
    errors: dict[UUID, str] = field(default_factory=dict)


async def bulk_transition(
    db: AsyncSession,
    *,
    orders: Iterable[Order],
    to_status: OrderStatus,
    actor: ActorRef,
) -> BulkResult:
    """Move multiple orders to ``to_status`` in a single pass.

    Delegates to :func:`transition_order` per order so the state-machine,
    history write, and timestamp side effects stay in one place. Domain
    errors (forbidden transitions, access denied) are captured into the
    returned :class:`BulkResult`; unexpected exceptions propagate so the
    caller can roll back.
    """
    result = BulkResult()
    for order in orders:
        try:
            await transition_order(db, order=order, to_status=to_status, actor=actor)
        except (ForbiddenTransition, ForbiddenActor, OrderAccessDenied, OrderError) as exc:
            result.errors[order.id] = str(exc) or exc.__class__.__name__
            continue
        result.succeeded.append(order.id)
    return result


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
