"""SLA service: on-time delivery rate + per-customer weekly heatmap.

All queries run inside the request's RLS-scoped session (``get_db`` in
``app.deps``), so tenant isolation is free — this module never passes a
``tenant_id`` filter around and never touches the owner role.

Semantics
---------
The KPI cards and the heatmap count **the same orders the same way**
(demo review P1-1: a "100 % on time" card sat next to a red heatmap).
Both go through :func:`_due_between` and :func:`_buckets`.

An order is *due* once its ``promised_delivery_at`` (a tenant-local
calendar day) is on or before ``today`` (also tenant-local). Only due,
non-cancelled orders whose promised date falls inside the window count.
Each lands in exactly one bucket:

* **on_time** — delivered, and ``delivered_at <= promised_delivery_at``.
* **late** — delivered, and ``delivered_at > promised_delivery_at``.
* **overdue** — still open (not delivered, closed or cancelled) and
  ``promised_delivery_at < today``: the promise is already broken even
  though nothing has shipped yet. Reported as ``pending`` on the card.
* **neutral** — everything else, not counted anywhere:

  - open with ``promised_delivery_at == today`` (it can still ship today);
  - an order whose promised date is in the future, *even if it was
    already delivered early* — it joins the report on its promised day,
    so the report never contains a week that has not happened yet;
  - **READY on time (P3-4)**: the order sits at READY and its history
    shows it *entered READY on or before the promised day* (the
    tenant-local calendar day of that transition). The goods were ready
    as promised and only the hand-over (pick-up, courier) is
    outstanding, so the promise is not broken. A READY order that became
    ready only *after* the promised day — or has no READY entry in its
    history — is overdue like any other open order. Once it is actually
    delivered it is judged by ``delivered_at`` like every delivered
    order, which may make it *late*;
  - a CLOSED order that never entered DELIVERED (see ``_IS_DELIVERED``).

``total = on_time + late + overdue`` and ``rate = on_time / total``
(``0.0`` when nothing was due). A heatmap cell uses the same formula, so
the cells of the heatmap add up to the card numbers.

"Delivered" means the order sits at DELIVERED, or at CLOSED with a
DELIVERED entry in its history — see ``_IS_DELIVERED`` for why
``delivered_at`` alone is not trusted.
"""

from __future__ import annotations

from datetime import date, timedelta, tzinfo
from typing import Any

from sqlalchemy import Date, DateTime, and_, case, cast, exists, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer
from app.models.enums import OrderStatus
from app.models.order import Order, OrderStatusHistory
from app.timezones import local_today, tz_label

#: Orders that count at all. A cancelled order was never going to be
#: delivered; counting it made the report read "late" for work the
#: customer called off (audit F-32 / 2026-10-03 LOGIC-19).
_NOT_CANCELLED = Order.status != OrderStatus.CANCELLED

#: "Really delivered": the order sits at DELIVERED, or at CLOSED *and*
#: its history shows it entered DELIVERED. ``delivered_at`` alone is not
#: enough — it survives backward corrections (CLAUDE.md §18) and is
#: backfilled on a DRAFT -> CLOSED bookkeeping jump where nothing was
#: delivered (LOGIC-21). Both would otherwise count as on-time deliveries.
_ENTERED_DELIVERED = exists(
    select(OrderStatusHistory.id).where(
        OrderStatusHistory.order_id == Order.id,
        OrderStatusHistory.to_status == OrderStatus.DELIVERED,
    )
)
_IS_DELIVERED = and_(
    Order.delivered_at.is_not(None),
    or_(
        Order.status == OrderStatus.DELIVERED,
        and_(Order.status == OrderStatus.CLOSED, _ENTERED_DELIVERED),
    ),
)
_STILL_OPEN = Order.status.notin_(
    (OrderStatus.DELIVERED, OrderStatus.CLOSED, OrderStatus.CANCELLED)
)


def _ready_in_time(tz: tzinfo | None) -> Any:
    """The order sits at READY and entered READY on/before its promised day.

    The transition instant is turned into the tenant's calendar day in
    SQL (``timezone(zone, ts)::date``) because the promised date is a
    local day, not an instant.
    """
    entered_ready_on = cast(func.timezone(tz_label(tz), OrderStatusHistory.created_at), Date)
    return and_(
        Order.status == OrderStatus.READY,
        exists(
            select(OrderStatusHistory.id).where(
                OrderStatusHistory.order_id == Order.id,
                OrderStatusHistory.to_status == OrderStatus.READY,
                entered_ready_on <= Order.promised_delivery_at,
            )
        ),
    )


def _buckets(today: date, tz: tzinfo | None) -> tuple[Any, Any, Any]:
    """``(on_time, late, overdue)`` CASE expressions, 1 or 0 per order."""
    on_time = case(
        (and_(_IS_DELIVERED, Order.delivered_at <= Order.promised_delivery_at), 1),
        else_=0,
    )
    late = case(
        (and_(_IS_DELIVERED, Order.delivered_at > Order.promised_delivery_at), 1),
        else_=0,
    )
    overdue = case(
        (
            and_(
                _STILL_OPEN,
                Order.promised_delivery_at < today,
                not_(_ready_in_time(tz)),
            ),
            1,
        ),
        else_=0,
    )
    return on_time, late, overdue


def _due_between(date_from: date, upper: date) -> Any:
    """The population shared by the cards and the heatmap."""
    return and_(
        Order.promised_delivery_at.is_not(None),
        Order.promised_delivery_at >= date_from,
        Order.promised_delivery_at <= upper,
        _NOT_CANCELLED,
    )


async def on_time_rate(
    db: AsyncSession,
    *,
    date_from: date,
    date_to: date,
    today: date | None = None,
    tz: tzinfo | None = None,
) -> dict:
    """Aggregate on-time / late / overdue counts for the window.

    Returns ``on_time``, ``late``, ``pending`` (overdue, still open),
    ``delivered`` (``on_time + late``), ``total`` (``delivered +
    pending`` — every order that was due) and ``rate`` (``on_time /
    total``, a float in ``[0, 1]``; ``0.0`` when nothing was due).

    The window is ``[date_from, min(date_to, today)]`` on the promised
    date — an order is not judged before its promised day. ``today`` and
    ``tz`` are the tenant's (default: today in ``DEFAULT_TIMEZONE``).
    """
    today = today or local_today(tz)
    on_time_expr, late_expr, overdue_expr = _buckets(today, tz)

    stmt = select(
        func.coalesce(func.sum(on_time_expr), 0).label("on_time"),
        func.coalesce(func.sum(late_expr), 0).label("late"),
        func.coalesce(func.sum(overdue_expr), 0).label("pending"),
    ).where(_due_between(date_from, min(date_to, today)))

    row = (await db.execute(stmt)).one()
    on_time = int(row.on_time)
    late = int(row.late)
    pending = int(row.pending)
    total = on_time + late + pending
    return {
        "on_time": on_time,
        "late": late,
        "pending": pending,
        "delivered": on_time + late,
        "total": total,
        "rate": (on_time / total) if total > 0 else 0.0,
    }


def _iso_week_start(d: date) -> date:
    """Monday of the ISO week containing ``d``."""
    return d - timedelta(days=d.weekday())


def heatmap_weeks(date_from: date, today: date) -> list[date]:
    """Every Monday from ``date_from``'s week up to the current week.

    The heatmap's column axis: continuous (a week without orders is an
    empty column, not a gap) and never in the future.
    """
    weeks: list[date] = []
    cursor = _iso_week_start(date_from)
    last = _iso_week_start(today)
    while cursor <= last:
        weeks.append(cursor)
        cursor += timedelta(weeks=1)
    return weeks


async def heatmap_data(
    db: AsyncSession,
    *,
    weeks: int = 12,
    today: date | None = None,
    date_from: date | None = None,
    tz: tzinfo | None = None,
) -> list[dict]:
    """Per-customer by-week aggregation for the heatmap view.

    Same population and buckets as :func:`on_time_rate`: pass the same
    ``date_from`` and the cells add up to the cards. Without
    ``date_from`` the window is the last ``weeks`` ISO weeks.

    A cell is (customer, Monday of the promised date's week) with
    ``on_time`` / ``late`` / ``overdue`` / ``total`` (the three summed).
    A customer whose orders in a week are all neutral gets no cell
    there. Sorted by (customer_name, week_start); the full column axis
    comes from :func:`heatmap_weeks`.
    """
    today = today or local_today(tz)
    if date_from is None:
        date_from = _iso_week_start(today) - timedelta(weeks=weeks - 1)

    # Monday of the promised date's week. The date goes through a plain
    # TIMESTAMP (no zone): ``date_trunc('week', <date>)`` alone promotes
    # it to TIMESTAMPTZ in the session zone, and a Prague-midnight Monday
    # came back as Sunday 22:00 UTC — every column a day early.
    week_start = cast(
        func.date_trunc("week", cast(Order.promised_delivery_at, DateTime())), Date
    ).label("week_start")
    on_time_expr, late_expr, overdue_expr = _buckets(today, tz)
    on_time_sum = func.coalesce(func.sum(on_time_expr), 0)
    late_sum = func.coalesce(func.sum(late_expr), 0)
    overdue_sum = func.coalesce(func.sum(overdue_expr), 0)

    stmt = (
        select(
            week_start,
            Customer.id.label("customer_id"),
            Customer.name.label("customer_name"),
            on_time_sum.label("on_time"),
            late_sum.label("late"),
            overdue_sum.label("overdue"),
        )
        .join(Customer, Customer.id == Order.customer_id)
        .where(_due_between(date_from, today))
        .group_by(week_start, Customer.id, Customer.name)
        .having((on_time_sum + late_sum + overdue_sum) > 0)
        .order_by(Customer.name, week_start)
    )

    rows = (await db.execute(stmt)).all()
    cells: list[dict] = []
    for row in rows:
        ws = row.week_start  # already a DATE (see the cast above)
        on_time, late, overdue = int(row.on_time), int(row.late), int(row.overdue)
        cells.append(
            {
                "week_start": ws,
                "customer_id": row.customer_id,
                "customer_name": row.customer_name,
                "on_time": on_time,
                "late": late,
                "overdue": overdue,
                "total": on_time + late + overdue,
            }
        )
    return cells
