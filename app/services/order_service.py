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

from sqlalchemy import Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer
from app.models.enums import OrderStatus
from app.models.order import Order, OrderComment, OrderItem, OrderStatusHistory
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit_service
from app.services.audit_service import SYSTEM_ACTOR, ActorInfo
from app.services.money import MONEY_MAX, AmountError, check_money, check_quantity, line_total


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


class InvalidAmount(OrderError):
    """A price or quantity failed :mod:`app.services.money` validation."""

    def __init__(self, error: AmountError) -> None:
        super().__init__(str(error))
        self.amount_error = error


class QuoteChanged(OrderError):
    """The quote moved between the customer reading it and confirming it.

    Raised by :func:`transition_order` when the caller passes the total it
    rendered (``expected_total``) and the live total differs. Nothing is
    written; the customer must look again.
    """


class IncompleteQuote(OrderError):
    """A quote/confirmation was attempted on an order that is not fully
    priced (or has no items at all)."""


class EmptyOrder(OrderError):
    """A customer tried to submit an order without a single line."""


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

# The manufacturing pipeline, in workflow order. ``CANCELLED`` is
# deliberately absent — it sits *beside* the pipeline rather than on it:
# reachable from any state and leading back to any state.
PIPELINE: tuple[OrderStatus, ...] = (
    OrderStatus.DRAFT,
    OrderStatus.SUBMITTED,
    OrderStatus.QUOTED,
    OrderStatus.CONFIRMED,
    OrderStatus.IN_PRODUCTION,
    OrderStatus.READY,
    OrderStatus.DELIVERED,
    OrderStatus.CLOSED,
)

_PIPELINE_RANK: dict[OrderStatus, int] = {s: i for i, s in enumerate(PIPELINE)}


def pipeline_rank(status: OrderStatus) -> int | None:
    """Position of ``status`` on the linear pipeline.

    Returns ``None`` for :attr:`OrderStatus.CANCELLED`, which is off-
    pipeline. Callers that order or compare statuses MUST handle the
    ``None`` case rather than defaulting to ``0`` — cancelling an order
    is not "moving it back to draft".
    """
    return _PIPELINE_RANK.get(status)


def skipped_statuses(from_status: OrderStatus, to_status: OrderStatus) -> list[OrderStatus]:
    """Pipeline steps jumped over by a forward move.

    ``DRAFT → CONFIRMED`` skips ``SUBMITTED`` and ``QUOTED``. Backward
    moves and moves involving ``CANCELLED`` as the *target* skip nothing.
    Reopening a cancelled order counts everything before the target as
    skipped, because we cannot know how far it had progressed before —
    the milestone backfill only fills blanks, so an order that really
    did pass through those states keeps its original stamps.
    """
    dst = _PIPELINE_RANK.get(to_status)
    if dst is None:
        return []
    src = _PIPELINE_RANK.get(from_status)
    if src is None:  # coming back from CANCELLED
        src = -1
    if dst <= src:
        return []
    return list(PIPELINE[src + 1 : dst])


# Allowed forward transitions for CUSTOMER CONTACTS. External portal
# users can only request changes that advance or cancel the workflow.
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

# Allowed transitions for STAFF (tenant users): **any status to any
# other status**.
#
# This was a one-step-forward / one-step-back graph until we traced the
# complaints about it. The restriction never modelled a real business
# rule — it was compensating for a bug. Leaping across the pipeline
# skipped the milestone side effects below (``submitted_at``,
# ``quoted_total``, ``delivered_at``), so a DRAFT → DELIVERED jump left
# the order invisible to the SLA report. Rather than keep paying for
# that with a straitjacket, ``_backfill_milestones`` now fills the
# blanks, and the graph is free.
#
# Real shops need this: an order agreed over the phone goes
# DRAFT → CONFIRMED in one move; a courier collecting straight off the
# machine goes IN_PRODUCTION → DELIVERED without a fake READY stop; a
# mistake found a week later is correctable without walking the chain
# back one state at a time.
#
# Customer contacts stay on the tight ``CONTACT_ALLOWED_TRANSITIONS``
# graph — the freedom here is an operator privilege, not a public one.
STAFF_ALLOWED_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    status: {other for other in OrderStatus if other is not status} for status in OrderStatus
}

# Legacy alias — retained so any caller that still imports ALL_STATUSES
# sees the full set for iteration but SHOULD NOT use it as a transition
# whitelist. Prefer STAFF_ALLOWED_TRANSITIONS.
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


#: Sentinel for "orders with nobody responsible" in the assignment
#: filter. A plain ``None`` already means "do not filter", so the
#: unassigned case needs its own value.
UNASSIGNED = "unassigned"


#: Default age (days) after which an unanswered quote counts as "waiting
#: too long" on the dashboard and gets a follow-up reminder (IDEA-1/2).
DEFAULT_STALE_QUOTE_DAYS = 3

#: Statuses in which a passed promised date no longer matters — the
#: goods are out, or the order is dead.
DONE_STATUSES: frozenset[OrderStatus] = frozenset(
    {OrderStatus.DELIVERED, OrderStatus.CLOSED, OrderStatus.CANCELLED}
)

#: Named "needs action" queues (IDEA-1). Each is a predicate on
#: ``orders`` shared by the dashboard counters and the filtered list the
#: counter links to, so the two can never disagree.
WORK_QUEUES: tuple[str, ...] = ("awaiting_quote", "no_promise", "overdue", "stale_quotes")


def build_orders_query(
    *,
    actor: ActorRef,
    status: OrderStatus | None = None,
    customer_id: UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    q: str | None = None,
    assigned_to: UUID | str | None = None,
    queue: str | None = None,
    sort: str | None = None,
    today: date | None = None,
    stale_quote_days: int = DEFAULT_STALE_QUOTE_DAYS,
) -> Select:
    """Build the base `SELECT orders` query shared by list + CSV export.

    Tenant isolation comes from RLS on the session. On top of that,
    customer contacts are constrained to their own customer's orders;
    the ``customer_id`` filter is applied only when the actor is staff.

    ``date_from`` / ``date_to`` are **inclusive** bounds compared against
    ``Order.created_at`` (truncated to a calendar date on the caller side
    by passing a ``date`` value). A ``None`` bound means "unbounded".

    ``assigned_to`` takes a user id, the :data:`UNASSIGNED` sentinel, or
    ``None`` for no filter. Staff-only, like ``customer_id`` — a contact
    has no business slicing the supplier's internal workload.

    ``queue`` names one of the "needs action" work queues
    (:data:`WORK_QUEUES`, staff only); ``sort`` is ``"due"`` / ``"-due"``
    for promised-date order (undated orders last) or ``None`` for
    newest first.

    Returns the base ``Select``; callers add ``.limit()`` / ``.offset()``.
    """
    if sort in ("due", "-due"):
        due = Order.promised_delivery_at
        stmt = select(Order).order_by(
            (due.asc() if sort == "due" else due.desc()).nulls_last(),
            Order.created_at.desc(),
        )
    else:
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
    if assigned_to is not None and actor.type != "contact":
        if assigned_to == UNASSIGNED:
            stmt = stmt.where(Order.assigned_to_user_id.is_(None))
        else:
            stmt = stmt.where(Order.assigned_to_user_id == assigned_to)
    if queue in WORK_QUEUES and actor.type != "contact":
        stmt = stmt.where(
            queue_predicate(queue, today=today or date.today(), stale_quote_days=stale_quote_days)
        )
    return stmt


def queue_predicate(queue: str, *, today: date, stale_quote_days: int = DEFAULT_STALE_QUOTE_DAYS):
    """SQL predicate for one of :data:`WORK_QUEUES`."""
    from datetime import timedelta

    if queue == "awaiting_quote":
        return Order.status == OrderStatus.SUBMITTED
    if queue == "no_promise":
        return and_(
            Order.status.in_((OrderStatus.CONFIRMED, OrderStatus.IN_PRODUCTION, OrderStatus.READY)),
            Order.promised_delivery_at.is_(None),
        )
    if queue == "overdue":
        return and_(
            Order.promised_delivery_at.is_not(None),
            Order.promised_delivery_at < today,
            Order.status.notin_(tuple(DONE_STATUSES)),
        )
    if queue == "stale_quotes":
        cutoff = datetime.now(UTC) - timedelta(days=max(0, stale_quote_days))
        return and_(
            Order.status == OrderStatus.QUOTED,
            Order.quoted_at.is_not(None),
            Order.quoted_at < cutoff,
        )
    raise ValueError(f"unknown queue {queue!r}")


def is_overdue(order: Order, today: date | None = None) -> bool:
    """True when the promised date has passed and the order is not done."""
    if order.promised_delivery_at is None or order.status in DONE_STATUSES:
        return False
    return order.promised_delivery_at < (today or date.today())


async def work_queue_counts(
    db: AsyncSession,
    *,
    today: date | None = None,
    stale_quote_days: int = DEFAULT_STALE_QUOTE_DAYS,
) -> dict[str, int]:
    """Counts for every staff work queue, in one round trip (IDEA-1)."""
    when = today or date.today()
    columns = [
        func.count()
        .filter(queue_predicate(name, today=when, stale_quote_days=stale_quote_days))
        .label(name)
        for name in WORK_QUEUES
    ]
    row = (await db.execute(select(*columns).select_from(Order))).one()
    return {name: int(getattr(row, name) or 0) for name in WORK_QUEUES}


async def list_orders_for_principal(
    db: AsyncSession,
    *,
    actor: ActorRef,
    status_filter: OrderStatus | None = None,
    customer_filter: UUID | None = None,
    search: str | None = None,
    assigned_filter: UUID | str | None = None,
    queue: str | None = None,
    sort: str | None = None,
    stale_quote_days: int = DEFAULT_STALE_QUOTE_DAYS,
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
        assigned_to=assigned_filter,
        queue=queue,
        sort=sort,
        stale_quote_days=stale_quote_days,
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
        if not customer.is_active:
            # Archived / blocked customer (LOGIC-15): history stays, new
            # work does not start. Unarchive first.
            raise OrderError("customer is archived")

    # Plan-limit gate (orders/month). Falls through for tenants on
    # community / unlimited plans.
    from app.platform.usage import ensure_within_limit

    # Soft for a customer contact (LOGIC-3) — see ensure_within_limit.
    await ensure_within_limit(
        db, tenant_id=tenant_id, metric="orders", soft=actor.type == "contact"
    )

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
    """``quantity * unit_price``, rounded half-up (LOGIC-17).

    Czech and German commercial practice rounds 0.125 to 0.13; the old
    default-context ``quantize`` used banker's rounding (0.12), a
    one-cent disagreement with the customer's ERP.
    """
    item.line_total = line_total(Decimal(item.quantity), item.unit_price)


def _validated_amounts(
    quantity: Decimal | None, unit_price: Decimal | None
) -> tuple[Decimal | None, Decimal | None]:
    """Defence-in-depth validation of an item's quantity and price.

    Routers already parse with :mod:`app.services.money`, but the service
    is the last line before a ``NaN`` reaches a ``numeric`` column — and
    from there every page that renders the order (LOGIC-1).
    """
    try:
        qty = check_quantity(quantity) if quantity is not None else None
        price = check_money(unit_price) if unit_price is not None else None
    except AmountError as exc:
        raise InvalidAmount(exc) from None
    return qty, price


async def _ensure_total_fits(
    db: AsyncSession,
    order: Order,
    new_line_total: Decimal | None,
    *,
    excluding_item_id: UUID | None = None,
) -> None:
    """Refuse a line whose addition would overflow ``orders.quoted_total``.

    Checked *before* the write: once a too-large sum is assigned to the
    ``Numeric(12, 2)`` cache the flush fails with a 500, and a route that
    redirects instead of raising would have committed the item already.
    """
    if new_line_total is None:
        return
    stmt = select(func.sum(OrderItem.line_total)).where(OrderItem.order_id == order.id)
    if excluding_item_id is not None:
        stmt = stmt.where(OrderItem.id != excluding_item_id)
    others = (await db.execute(stmt)).scalar() or Decimal("0")
    if Decimal(others) + new_line_total > MONEY_MAX:
        raise InvalidAmount(AmountError("price", "too_large"))


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
    audit_actor: ActorInfo | None = None,
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
    if quantity is None:
        raise OrderError("quantity must be positive")
    # Same rules as ``update_item``: finite, quantity > 0, price >= 0 and
    # within the column range. ``add_item`` used to have no sign or
    # finiteness check at all, so it accepted lines ``update_item`` then
    # refused to edit.
    checked_quantity, unit_price = _validated_amounts(quantity, unit_price)
    if checked_quantity is None:  # unreachable: checked above; narrows the type
        raise OrderError("quantity must be positive")
    quantity = checked_quantity
    try:
        new_total = line_total(quantity, unit_price)
    except AmountError as exc:
        raise InvalidAmount(exc) from None
    await _ensure_total_fits(db, order, new_total)

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
        quantity=quantity,
        unit=(unit or "ks")[:16],
        unit_price=unit_price,
        notes=notes or None,
    )
    _recalculate_line_total(item)
    db.add(item)
    await db.flush()

    # ``remove_item`` and ``update_item`` both do this; ``add_item`` did
    # not, so adding a line to an already-quoted order left the cached
    # total stale. The PDF prefers the cache over its own item table, so
    # it printed a Subtotal that contradicted the lines above it.
    await _recompute_quoted_total(db, order)

    await audit_service.record(
        db,
        action="order.item_added",
        entity_type="order",
        entity_id=order.id,
        entity_label=order.number,
        actor=audit_actor or SYSTEM_ACTOR,
        after={
            "item_id": str(item.id),
            "description": item.description,
            "quantity": str(item.quantity),
            "unit": item.unit,
            "unit_price": str(item.unit_price) if item.unit_price is not None else None,
        },
        tenant_id=tenant_id,
    )
    return item


async def remove_item(
    db: AsyncSession,
    *,
    order: Order,
    item: OrderItem,
    actor: ActorRef,
    audit_actor: ActorInfo | None = None,
) -> None:
    _ensure_item_editable(order, actor)
    snapshot = {
        "item_id": str(item.id),
        "description": item.description,
        "quantity": str(item.quantity),
        "unit": item.unit,
        "unit_price": str(item.unit_price) if item.unit_price is not None else None,
    }
    await db.delete(item)
    await db.flush()
    # Recompute the cached quoted total — the deleted line no longer
    # contributes. Keeps the dashboard card in sync with the items list.
    await _recompute_quoted_total(db, order)
    await audit_service.record(
        db,
        action="order.item_removed",
        entity_type="order",
        entity_id=order.id,
        entity_label=order.number,
        actor=audit_actor or SYSTEM_ACTOR,
        before=snapshot,
        tenant_id=order.tenant_id,
    )


async def update_item(
    db: AsyncSession,
    *,
    order: Order,
    item: OrderItem,
    quantity: Decimal | None = None,
    unit_price: Decimal | None = None,
    note: str | None = None,
    actor: ActorRef | None = None,
    audit_actor: ActorInfo | None = None,
) -> OrderItem:
    """Partially update a line item on an editable order.

    Only provided (non-``None``) fields are applied — this mirrors the
    field-level autosave UX where a single input change PATCHes just that
    field. Editability follows :func:`_ensure_item_editable`: contacts can
    only edit in DRAFT, staff in DRAFT / SUBMITTED / QUOTED. Past QUOTED
    the order is contractually agreed; corrections go through a transition
    instead.

    ``actor`` is optional so existing background-task callers without a
    principal still work; in that case we apply the staff state-rules.
    """
    if actor is not None:
        _ensure_item_editable(order, actor)
        # Contact scope check — cannot patch somebody else's customer's order.
        if actor.type == "contact" and order.customer_id != actor.customer_id:
            raise OrderAccessDenied()
    elif order.status not in STAFF_ITEM_EDIT_STATES:
        raise ForbiddenTransition(
            "items can only be edited while the order is draft, submitted or quoted"
        )

    before = {
        "quantity": str(item.quantity),
        "unit_price": str(item.unit_price) if item.unit_price is not None else None,
        "notes": item.notes,
    }

    # Validate everything before touching the row, so a rejected value
    # leaves the item exactly as it was. ``None`` means "do not touch";
    # there is no sentinel for clearing a price.
    quantity, unit_price = _validated_amounts(quantity, unit_price)
    new_quantity = quantity if quantity is not None else item.quantity
    new_price = unit_price if unit_price is not None else item.unit_price
    try:
        new_total = line_total(Decimal(new_quantity), new_price)
    except AmountError as exc:
        raise InvalidAmount(exc) from None
    await _ensure_total_fits(db, order, new_total, excluding_item_id=item.id)

    if quantity is not None:
        item.quantity = quantity

    if unit_price is not None:
        item.unit_price = unit_price

    if note is not None:
        # Empty string clears the note; treat "   " as empty too.
        cleaned = note.strip() or None
        item.notes = cleaned

    _recalculate_line_total(item)
    await db.flush()
    # Keep ``quoted_total`` aligned with the sum of line totals so the
    # header card on the detail page reflects the just-saved change.
    await _recompute_quoted_total(db, order)

    after = {
        "quantity": str(item.quantity),
        "unit_price": str(item.unit_price) if item.unit_price is not None else None,
        "notes": item.notes,
    }
    await audit_service.record(
        db,
        action="order.item_updated",
        entity_type="order",
        entity_id=order.id,
        entity_label=order.number,
        actor=audit_actor or SYSTEM_ACTOR,
        before=before,
        after=after,
        tenant_id=order.tenant_id,
    )
    return item


# States in which staff may still edit items / set prices. Lets the
# supplier add unit prices on a SUBMITTED order before transitioning it
# to QUOTED, and keep correcting line totals while the order sits in
# QUOTED waiting for the customer's confirmation. Past QUOTED the order
# is contractually agreed — line items become append-only and price-locked.
STAFF_ITEM_EDIT_STATES: frozenset[OrderStatus] = frozenset(
    {OrderStatus.DRAFT, OrderStatus.SUBMITTED, OrderStatus.QUOTED}
)


def _ensure_item_editable(order: Order, actor: ActorRef) -> None:
    """Check whether the actor can add/remove/edit items on the order.

    - Customer contacts: DRAFT only.
    - Staff: DRAFT, SUBMITTED, or QUOTED — see :data:`STAFF_ITEM_EDIT_STATES`.
    """
    if actor.type == "contact":
        if order.status != OrderStatus.DRAFT:
            raise ForbiddenTransition("customer contacts may only edit items in DRAFT")
        return
    if order.status not in STAFF_ITEM_EDIT_STATES:
        raise ForbiddenTransition(
            "items can only be edited while the order is draft, submitted or quoted"
        )


async def _recompute_quoted_total(db: AsyncSession, order: Order) -> None:
    """Refresh the cached ``quoted_total`` from the current line items.

    ``func.sum`` returns NULL when no row has a ``line_total`` — i.e.
    the order has no items, or none of them carry a unit price. That is
    genuinely "no quote yet", so it must stay NULL rather than collapse
    to 0.00: a stamped 0.00 renders as "0 Kč" on the customer-facing PDF
    and the detail card, which reads as a free order rather than an
    unpriced one. Only ``coalesce`` when there is something to sum.
    """
    total = (
        await db.execute(
            select(func.sum(OrderItem.line_total)).where(OrderItem.order_id == order.id)
        )
    ).scalar()
    order.quoted_total = Decimal(total) if total is not None else None
    await db.flush()


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


def _stamp_confirmation(order: Order, *, actor: ActorRef, now: datetime) -> None:
    """Snapshot the agreed amount and who agreed to it (LOGIC-2).

    ``quoted_total`` is a live cache that is re-summed on every item
    edit; it cannot prove what the customer accepted. This copy can.
    """
    order.confirmed_total = order.quoted_total
    order.confirmed_at = now
    order.confirmed_by_user_id = actor.id if actor.type == "user" else None
    order.confirmed_by_contact_id = actor.id if actor.type == "contact" else None


async def _backfill_milestones(
    db: AsyncSession,
    order: Order,
    *,
    to_status: OrderStatus,
    now: datetime,
    actor: ActorRef | None = None,
) -> None:
    """Fill in milestone data for pipeline steps the order jumped over.

    Staff may move an order to any status (see
    ``STAFF_ALLOWED_TRANSITIONS``), which means the per-status side
    effects below can no longer be relied on to fire in sequence. An
    order sent DRAFT → DELIVERED never passes through SUBMITTED, so
    without this it would carry ``submitted_at IS NULL`` and — worse —
    ``delivered_at IS NULL``, dropping it out of the SLA report in
    ``app.services.sla_service`` entirely.

    The rule is **fill blanks only, never overwrite**. A stamp that
    already exists is the real one, recorded when the order genuinely
    passed that milestone; a backfilled stamp is a best-effort "it must
    have happened by now". Overwriting would rewrite history and move
    SLA numbers under the operator's feet.
    """
    dst = _PIPELINE_RANK.get(to_status)
    if dst is None:  # CANCELLED — off-pipeline, nothing to backfill
        return

    if dst >= _PIPELINE_RANK[OrderStatus.SUBMITTED] and order.submitted_at is None:
        order.submitted_at = now
    if dst >= _PIPELINE_RANK[OrderStatus.QUOTED] and order.quoted_total is None:
        await _recompute_quoted_total(db, order)
    if dst >= _PIPELINE_RANK[OrderStatus.QUOTED] and order.quoted_at is None:
        order.quoted_at = now
    # A phone-agreed DRAFT → IN_PRODUCTION jump is still an agreement:
    # snapshot whatever total stood at that moment, attributed to the
    # staff member who made the jump.
    if (
        dst >= _PIPELINE_RANK[OrderStatus.CONFIRMED]
        and order.confirmed_at is None
        and actor is not None
    ):
        _stamp_confirmation(order, actor=actor, now=now)
    if dst >= _PIPELINE_RANK[OrderStatus.DELIVERED] and order.delivered_at is None:
        order.delivered_at = now.date()
    if dst >= _PIPELINE_RANK[OrderStatus.CLOSED] and order.closed_at is None:
        order.closed_at = now

    # NOTE: backward moves deliberately do NOT retract milestone stamps.
    # The 2026-07-26 audit flagged this (F-31) on the grounds that a
    # DRAFT carrying "Delivered 12.03." contradicts itself. It is a fair
    # observation, but keeping the stamp is the older and stronger
    # decision, pinned by
    # tests/test_orders_transition_delivered_at.py: the date records
    # when the goods actually left, and clearing it on a correction
    # would either lose that fact or re-stamp a later, wrong date when
    # the order re-enters DELIVERED. Fill blanks; never rewrite history.
    #
    # The 2026-10-03 audit (LOGIC-21) re-raised the DRAFT → CLOSED case:
    # the jump backfills ``delivered_at`` although nothing was delivered.
    # The stamp stays (CLAUDE.md §18); instead ``sla_service`` refuses to
    # count a CLOSED order whose history never entered DELIVERED, so the
    # backfilled date cannot reach the on-time metric.


#: Sentinel for :func:`transition_order`'s ``expected_total`` — "the
#: caller did not render a total, do not compare". ``None`` is a real
#: value there (an unpriced quote), so it cannot double as "no check".
NO_TOTAL_CHECK: object = object()


def _same_total(a: Decimal | None, b: Decimal | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return Decimal(a).quantize(Decimal("0.01")) == Decimal(b).quantize(Decimal("0.01"))


async def quote_problems(db: AsyncSession, order: Order) -> list[str]:
    """Why ``order`` cannot be quoted or confirmed as it stands.

    Returns machine codes — ``"no_items"`` or ``"unpriced_items"`` — so
    the router can word them and the stepper can warn *before* the
    click. Empty list = fully priced, at least one line.
    """
    rows = (
        await db.execute(select(OrderItem.unit_price).where(OrderItem.order_id == order.id))
    ).all()
    if not rows:
        return ["no_items"]
    if any(row[0] is None for row in rows):
        return ["unpriced_items"]
    return []


async def transition_order(
    db: AsyncSession,
    *,
    order: Order,
    to_status: OrderStatus,
    actor: ActorRef,
    note: str | None = None,
    audit_actor: ActorInfo | None = None,
    expected_total: object = NO_TOTAL_CHECK,
    allow_incomplete: bool = False,
    promised_delivery_at: date | None = None,
    incomplete_note: str = "sent with unpriced items",
) -> Order:
    """Move the order to `to_status` after validating the move.

    Staff may move an order to any other status; customer contacts are
    held to the tight ``CONTACT_ALLOWED_TRANSITIONS`` graph. Milestone
    data for any pipeline step jumped over is backfilled — see
    :func:`_backfill_milestones`.

    The row is locked before the guard runs. Without it a double-click
    (or two operators on the same order) had both requests read the same
    starting status, both pass the transition check, and both write a
    history row and fire a customer email — so the client received two
    "your order is ready" messages for one move.

    Content guards (audit 2026-10-03), all checked under the lock:

    * a contact cannot **submit** an order with no items (LOGIC-7);
    * landing on **QUOTED** or **CONFIRMED** needs at least one item and
      every line priced (LOGIC-7). Contacts are always held to it; staff
      may pass ``allow_incomplete=True`` — the stepper only does so after
      an explicit "send anyway" confirm, and the history note records it.
      Jumps *past* CONFIRMED are not guarded: a phone-agreed job may go
      straight into production (CLAUDE.md §18);
    * ``expected_total`` (the total the confirming page rendered) must
      equal the live total when landing on **CONFIRMED**, or
      :class:`QuoteChanged` is raised and nothing is written (LOGIC-2).

    Landing on CONFIRMED snapshots ``confirmed_total`` / ``confirmed_at``
    / ``confirmed_by_*``; ``promised_delivery_at`` (staff only) is stored
    when given (IDEA-4).
    """
    # SELECT ... FOR UPDATE on this order only; serialises concurrent
    # transitions without touching readers elsewhere.
    await db.execute(select(Order.id).where(Order.id == order.id).with_for_update())
    await db.refresh(order)

    if order.status == to_status:
        raise ForbiddenTransition("already in that status")

    if actor.type == "contact":
        allowed = CONTACT_ALLOWED_TRANSITIONS.get(order.status, set())
        if order.customer_id != actor.customer_id:
            raise OrderAccessDenied()
    else:
        allowed = STAFF_ALLOWED_TRANSITIONS.get(order.status, set())

    if to_status not in allowed:
        raise ForbiddenTransition(
            f"cannot transition from {order.status.value} to {to_status.value}"
        )

    if promised_delivery_at is not None and actor.type != "user":
        raise ForbiddenActor("only staff can promise a delivery date")

    if (
        to_status == OrderStatus.SUBMITTED
        and actor.type == "contact"
        and "no_items" in await quote_problems(db, order)
    ):
        raise EmptyOrder("an order needs at least one item before it is submitted")

    override_note: str | None = None
    if to_status in (OrderStatus.QUOTED, OrderStatus.CONFIRMED):
        problems = await quote_problems(db, order)
        if problems:
            if actor.type == "contact" or not allow_incomplete:
                raise IncompleteQuote(problems[0])
            # Recorded in the history so the override is never silent.
            override_note = incomplete_note

    if to_status == OrderStatus.CONFIRMED and expected_total is not NO_TOTAL_CHECK:
        # Compare against a freshly summed total, not the cache — the
        # cache is what a concurrent edit may just have moved.
        await _recompute_quoted_total(db, order)
        if not _same_total(order.quoted_total, expected_total):  # type: ignore[arg-type]
            raise QuoteChanged("the quote changed since it was displayed")

    now = datetime.now(UTC)
    previous = order.status
    skipped = skipped_statuses(previous, to_status)
    order.status = to_status

    if promised_delivery_at is not None:
        order.promised_delivery_at = promised_delivery_at

    # Side effects for landing *exactly* on a status. These re-stamp on
    # every hit (a re-submitted order gets a fresh ``submitted_at``),
    # which is why they run before the fill-blanks-only backfill.
    if to_status == OrderStatus.SUBMITTED:
        order.submitted_at = now
    if to_status == OrderStatus.QUOTED:
        # Make sure we have a total computed from the item prices.
        await _recompute_quoted_total(db, order)
        # A re-quote is a new offer: restart the follow-up clock.
        order.quoted_at = now
    if to_status == OrderStatus.CONFIRMED:
        await _recompute_quoted_total(db, order)
        _stamp_confirmation(order, actor=actor, now=now)
    if to_status == OrderStatus.DELIVERED and order.delivered_at is None:
        # Stamp only once — if staff toggle DELIVERED off and back on, we
        # keep the original delivery date so SLA numbers remain stable.
        order.delivered_at = date.today()
    if to_status == OrderStatus.CLOSED:
        order.closed_at = now
    if to_status == OrderStatus.CANCELLED:
        order.cancelled_at = now

    await _backfill_milestones(db, order, to_status=to_status, now=now, actor=actor)

    history_note = "; ".join(part for part in ((note or "").strip(), override_note) if part)

    db.add(
        OrderStatusHistory(
            tenant_id=order.tenant_id,
            order_id=order.id,
            from_status=previous,
            to_status=to_status,
            changed_by_user_id=actor.id if actor.type == "user" else None,
            changed_by_contact_id=actor.id if actor.type == "contact" else None,
            note=history_note or None,
        )
    )
    await db.flush()

    after: dict = {
        "status": to_status.value,
        # Machine-readable record of a multi-step jump, so the audit
        # log can explain why an order shows DRAFT → DELIVERED and
        # which stamps were backfilled rather than observed.
        **({"skipped": [s.value for s in skipped]} if skipped else {}),
    }
    if to_status == OrderStatus.CONFIRMED:
        # The amount the customer agreed to, in the tamper-evident log.
        after["confirmed_total"] = (
            str(order.confirmed_total) if order.confirmed_total is not None else None
        )
    if promised_delivery_at is not None:
        after["promised_delivery_at"] = promised_delivery_at.isoformat()
    if history_note:
        after["note"] = history_note

    await audit_service.record(
        db,
        action="order.status_changed",
        entity_type="order",
        entity_id=order.id,
        entity_label=order.number,
        actor=audit_actor or SYSTEM_ACTOR,
        before={"status": previous.value},
        after=after,
        tenant_id=order.tenant_id,
    )
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
    audit_actor: ActorInfo | None = None,
) -> BulkResult:
    """Move multiple orders to ``to_status`` in a single pass.

    Delegates to :func:`transition_order` per order so the state-machine,
    history write, and timestamp side effects stay in one place. Domain
    errors (forbidden transitions, access denied) are captured into the
    returned :class:`BulkResult`; unexpected exceptions propagate so the
    caller can roll back.

    ``audit_actor`` must be forwarded: without it every order touched by
    a bulk change was attributed to ``system`` in the audit log, so the
    one operation most likely to need an explanation — "who moved
    twenty orders to Delivered?" — was the one it could not answer.
    """
    result = BulkResult()
    for order in orders:
        try:
            await transition_order(
                db,
                order=order,
                to_status=to_status,
                actor=actor,
                audit_actor=audit_actor,
            )
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
    audit_actor: ActorInfo | None = None,
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

    # Keep the diff small — long bodies are kept in the source table.
    excerpt = body if len(body) <= 200 else body[:197] + "..."
    await audit_service.record(
        db,
        action="order.comment_added",
        entity_type="order",
        entity_id=order.id,
        entity_label=order.number,
        actor=audit_actor or SYSTEM_ACTOR,
        after={
            "comment_id": str(comment.id),
            "is_internal": is_internal,
            "body": excerpt,
        },
        tenant_id=tenant_id,
    )
    return comment


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------


async def assign_order(
    db: AsyncSession,
    *,
    order: Order,
    assignee_id: UUID | None,
    actor: ActorRef,
    audit_actor: ActorInfo | None = None,
) -> User | None:
    """Set (or clear) the staff member responsible for ``order``.

    Returns the newly assigned :class:`~app.models.user.User`, or ``None``
    when the order was unassigned. Returns early — without writing or
    auditing — if the assignment is unchanged, so re-submitting the form
    does not spam the audit log or re-notify the assignee.

    Only staff may assign. The assignee lookup runs on the RLS-scoped
    session, so a forged user id from another tenant simply resolves to
    nothing and raises rather than leaking the row's existence.
    """
    if actor.type != "user":
        raise ForbiddenActor("only tenant staff can assign orders")

    if order.assigned_to_user_id == assignee_id:
        return None

    assignee: User | None = None
    if assignee_id is not None:
        assignee = (
            await db.execute(select(User).where(User.id == assignee_id))
        ).scalar_one_or_none()
        # ``password_hash IS NULL`` means invited but never accepted. The
        # picker already hides them (list_assignable_staff); the rule has
        # to live here too, or a direct POST assigns work to somebody who
        # cannot log in — and, once they own the order, colleagues scoped
        # to "only mine" filter themselves out of it.
        if assignee is None or not assignee.is_active or assignee.password_hash is None:
            raise OrderError("unknown or inactive assignee")

    previous_id = order.assigned_to_user_id
    order.assigned_to_user_id = assignee_id
    await db.flush()

    await audit_service.record(
        db,
        action="order.assigned",
        entity_type="order",
        entity_id=order.id,
        entity_label=order.number,
        actor=audit_actor or SYSTEM_ACTOR,
        before={"assigned_to_user_id": str(previous_id) if previous_id else None},
        after={"assigned_to_user_id": str(assignee_id) if assignee_id else None},
        tenant_id=order.tenant_id,
    )
    return assignee


async def list_assignable_staff(db: AsyncSession) -> list[User]:
    """Active staff users who can own an order, for the assignment picker.

    Both roles: an Operator is exactly the person a job should be
    assigned to. Invited-but-not-accepted rows (``password_hash IS NULL``)
    are excluded — assigning work to somebody who cannot log in yet just
    hides it.
    """
    stmt = (
        select(User)
        .where(User.is_active.is_(True), User.password_hash.is_not(None))
        .order_by(User.full_name)
    )
    return list((await db.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# Price memory (IDEA-6)
# ---------------------------------------------------------------------------


async def last_prices_for_customer(
    db: AsyncSession,
    *,
    customer_id: UUID,
    product_ids: list[UUID],
    exclude_order_id: UUID | None = None,
) -> dict[UUID, tuple[Decimal, datetime]]:
    """Most recent priced line per product on this customer's orders.

    Quoting is the supplier's most repetitive task; showing (and
    pre-filling) "last quoted to this customer: 12,40 Kč" saves a trip
    through old PDFs. Cancelled orders are ignored — a price nobody
    accepted is not a precedent. One query, ``DISTINCT ON`` product.
    """
    if not product_ids:
        return {}
    stmt = (
        select(OrderItem.product_id, OrderItem.unit_price, OrderItem.created_at)
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            Order.customer_id == customer_id,
            Order.status != OrderStatus.CANCELLED,
            OrderItem.product_id.in_(product_ids),
            OrderItem.unit_price.is_not(None),
        )
        .order_by(OrderItem.product_id, OrderItem.created_at.desc())
        .distinct(OrderItem.product_id)
    )
    if exclude_order_id is not None:
        stmt = stmt.where(OrderItem.order_id != exclude_order_id)
    rows = (await db.execute(stmt)).all()
    return {
        row.product_id: (row.unit_price, row.created_at)
        for row in rows
        if row.unit_price is not None and Decimal(row.unit_price).is_finite()
    }


# ---------------------------------------------------------------------------
# Order header edit (LOGIC-12)
# ---------------------------------------------------------------------------

#: The customer may be changed only before the supplier has committed to
#: anything on the order — a quote names the customer.
CUSTOMER_EDIT_STATES: frozenset[OrderStatus] = frozenset({OrderStatus.DRAFT, OrderStatus.SUBMITTED})

_HEADER_FIELDS = ("title", "customer_id", "requested_delivery_at", "promised_delivery_at", "notes")


async def update_order_header(
    db: AsyncSession,
    *,
    order: Order,
    actor: ActorRef,
    title: str,
    customer_id: UUID,
    requested_delivery_at: date | None,
    promised_delivery_at: date | None,
    notes: str | None,
    audit_actor: ActorInfo | None = None,
) -> list[str]:
    """Correct an order's header (staff only). Returns the changed fields.

    Until now nothing could change ``title``, ``customer_id``,
    ``requested_delivery_at`` or ``promised_delivery_at`` after creation:
    a mis-picked customer burned the order number, and the promised date
    the SLA report reads had no writer at all.
    """
    if actor.type != "user":
        raise ForbiddenActor("only tenant staff can edit the order header")

    title = (title or "").strip()
    if not title:
        raise OrderError("title is required")

    if customer_id != order.customer_id:
        if order.status not in CUSTOMER_EDIT_STATES:
            raise ForbiddenTransition("the client can only be changed before the order is quoted")
        exists_row = (
            await db.execute(select(Customer.id).where(Customer.id == customer_id))
        ).scalar_one_or_none()
        if exists_row is None:
            raise OrderError("unknown customer")

    before = {name: getattr(order, name) for name in _HEADER_FIELDS}
    order.title = title[:255]
    order.customer_id = customer_id
    order.requested_delivery_at = requested_delivery_at
    order.promised_delivery_at = promised_delivery_at
    order.notes = (notes or "").strip() or None
    after = {name: getattr(order, name) for name in _HEADER_FIELDS}

    changed = [name for name in _HEADER_FIELDS if before[name] != after[name]]
    if not changed:
        return []
    await db.flush()
    await audit_service.record(
        db,
        action="order.updated",
        entity_type="order",
        entity_id=order.id,
        entity_label=order.number,
        actor=audit_actor or SYSTEM_ACTOR,
        before={name: before[name] for name in changed},
        after={name: after[name] for name in changed},
        tenant_id=order.tenant_id,
    )
    return changed


# ---------------------------------------------------------------------------
# Order again (IDEA-3)
# ---------------------------------------------------------------------------


async def duplicate_order(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    source: Order,
    actor: ActorRef,
    include_catalog: bool = True,
    audit_actor: ActorInfo | None = None,
) -> Order:
    """Copy ``source``'s lines into a new DRAFT for the same customer.

    Job-shop customers reorder the same parts constantly. The copy keeps
    description, quantity, unit, product link and line note. Prices are
    **not** carried over from the old order — the supplier re-quotes:
    a catalog line takes the product's *current* list price (the same
    rule as adding it fresh), a free-text line starts unpriced.
    Attachments are not copied; drawings are re-attached deliberately.

    ``include_catalog=False`` (a contact without catalog access) drops the
    product link and its list price, mirroring the add-item permission.
    """
    from app.models.product import Product

    if actor.type == "contact" and source.customer_id != actor.customer_id:
        raise OrderAccessDenied()

    order = await create_order(
        db,
        tenant_id=tenant_id,
        actor=actor,
        customer_id=source.customer_id,
        title=source.title,
        notes=source.notes,
    )

    items = await list_items(db, source.id)
    product_ids = [i.product_id for i in items if i.product_id is not None]
    products: dict = {}
    if include_catalog and product_ids:
        rows = (
            await db.execute(
                select(Product).where(
                    Product.id.in_(product_ids),
                    Product.is_active.is_(True),
                )
            )
        ).scalars()
        products = {
            p.id: p for p in rows if p.customer_id is None or p.customer_id == source.customer_id
        }

    for position, item in enumerate(items):
        product = products.get(item.product_id) if item.product_id else None
        price = product.default_price if product is not None else None
        copy = OrderItem(
            tenant_id=tenant_id,
            order_id=order.id,
            product_id=product.id if product is not None else None,
            position=position,
            description=item.description,
            quantity=item.quantity,
            unit=item.unit,
            unit_price=price,
            notes=item.notes,
        )
        _recalculate_line_total(copy)
        db.add(copy)
    await db.flush()
    await _recompute_quoted_total(db, order)

    await audit_service.record(
        db,
        action="order.duplicated",
        entity_type="order",
        entity_id=order.id,
        entity_label=order.number,
        actor=audit_actor or SYSTEM_ACTOR,
        after={"source_order_id": str(source.id), "source_number": source.number},
        tenant_id=tenant_id,
    )
    return order
