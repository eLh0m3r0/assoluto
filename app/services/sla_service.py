"""SLA service: on-time delivery rate + per-customer weekly heatmap.

All queries run inside the request's RLS-scoped session (``get_db`` in
``app.deps``), so tenant isolation is free — this module never passes a
``tenant_id`` filter around and never touches the owner role.

Semantics
---------
An order contributes to the SLA window when its ``promised_delivery_at``
falls inside ``[date_from, date_to]`` (inclusive). We bucket each such
order into exactly one of:

* **on_time** — ``delivered_at`` is set and ``delivered_at <=
  promised_delivery_at``.
* **late** — ``delivered_at`` is set and ``delivered_at >
  promised_delivery_at``.
* **pending** — ``delivered_at`` is NULL and ``promised_delivery_at <
  today()`` (i.e. overdue, still open, counted separately so it does
  not inflate the "late" bucket until it actually ships).

Orders whose promised date is still in the future and are not yet
delivered are ignored entirely — they have neither happened nor slipped.

The ``rate`` is computed over delivered orders only
(``on_time / (on_time + late)``) — pending orders are surfaced in the UI
as their own count but do not poison the on-time ratio.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer
from app.models.order import Order


async def on_time_rate(
    db: AsyncSession,
    *,
    date_from: date,
    date_to: date,
) -> dict:
    """Aggregate on-time / late / pending counts for the window.

    Returns a dict with ``total`` (delivered in-window), ``on_time``,
    ``late``, ``pending`` (overdue undelivered), and ``rate`` (float in
    ``[0, 1]`` — 0.0 when there are no delivered orders in the window).
    """
    today = date.today()

    in_window = and_(
        Order.promised_delivery_at.is_not(None),
        Order.promised_delivery_at >= date_from,
        Order.promised_delivery_at <= date_to,
    )

    on_time_expr = case(
        (
            and_(
                Order.delivered_at.is_not(None),
                Order.delivered_at <= Order.promised_delivery_at,
            ),
            1,
        ),
        else_=0,
    )
    late_expr = case(
        (
            and_(
                Order.delivered_at.is_not(None),
                Order.delivered_at > Order.promised_delivery_at,
            ),
            1,
        ),
        else_=0,
    )
    pending_expr = case(
        (
            and_(
                Order.delivered_at.is_(None),
                Order.promised_delivery_at < today,
            ),
            1,
        ),
        else_=0,
    )

    stmt = select(
        func.coalesce(func.sum(on_time_expr), 0).label("on_time"),
        func.coalesce(func.sum(late_expr), 0).label("late"),
        func.coalesce(func.sum(pending_expr), 0).label("pending"),
    ).where(in_window)

    row = (await db.execute(stmt)).one()
    on_time = int(row.on_time)
    late = int(row.late)
    pending = int(row.pending)
    delivered_total = on_time + late
    rate = (on_time / delivered_total) if delivered_total > 0 else 0.0

    return {
        "total": delivered_total,
        "on_time": on_time,
        "late": late,
        "pending": pending,
        "rate": rate,
    }


def _iso_week_start(d: date) -> date:
    """Monday of the ISO week containing ``d``."""
    return d - timedelta(days=d.weekday())


async def heatmap_data(
    db: AsyncSession,
    *,
    weeks: int = 12,
) -> list[dict]:
    """Per-customer by-week aggregation for the heatmap view.

    Groups by customer and the Monday of the ISO week of
    ``promised_delivery_at``, so each cell carries on-time/late/total
    counts for that (customer, week). Rows with no promised date are
    skipped — they can't be on or off SLA.

    The list is sorted by (customer_name, week_start) so the router can
    pivot it into a grid without re-sorting.
    """
    today = date.today()
    start_week = _iso_week_start(today) - timedelta(weeks=weeks - 1)

    # date_trunc('week', ...) returns the Monday at 00:00 — perfect for
    # grouping. We cast to Date on the Python side below.
    week_start = func.date_trunc("week", Order.promised_delivery_at).label("week_start")

    on_time_expr = case(
        (
            and_(
                Order.delivered_at.is_not(None),
                Order.delivered_at <= Order.promised_delivery_at,
            ),
            1,
        ),
        else_=0,
    )
    late_expr = case(
        (
            and_(
                Order.delivered_at.is_not(None),
                Order.delivered_at > Order.promised_delivery_at,
            ),
            1,
        ),
        else_=0,
    )

    stmt = (
        select(
            week_start,
            Customer.id.label("customer_id"),
            Customer.name.label("customer_name"),
            func.coalesce(func.sum(on_time_expr), 0).label("on_time"),
            func.coalesce(func.sum(late_expr), 0).label("late"),
            func.count(Order.id).label("total"),
        )
        .join(Customer, Customer.id == Order.customer_id)
        .where(
            Order.promised_delivery_at.is_not(None),
            Order.promised_delivery_at >= start_week,
        )
        .group_by(week_start, Customer.id, Customer.name)
        .order_by(Customer.name, week_start)
    )

    rows = (await db.execute(stmt)).all()
    cells: list[dict] = []
    for row in rows:
        raw_ws = row.week_start
        # date_trunc returns a datetime in PG; normalize to date.
        ws = raw_ws.date() if hasattr(raw_ws, "date") else raw_ws
        cells.append(
            {
                "week_start": ws,
                "customer_id": row.customer_id,
                "customer_name": row.customer_name,
                "on_time": int(row.on_time),
                "late": int(row.late),
                "total": int(row.total),
            }
        )
    return cells
