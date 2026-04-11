"""Periodic background jobs driven by APScheduler.

Each job takes its own DB session (across all tenants — they query the
`tenants` table, which isn't RLS-protected, and use `row_security = off`
to bypass per-tenant policies when they need to touch tenant-scoped
tables across the board).

A Postgres advisory lock (pg_try_advisory_lock) wraps each job so that
running multiple web workers won't cause the same job to execute twice.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.logging import get_logger
from app.models.enums import OrderStatus
from app.models.order import Order, OrderStatusHistory

log = get_logger("app.tasks.periodic")

AUTO_CLOSE_LOCK_ID = 42_001
AUTO_CLOSE_AFTER_DAYS = 14


def _owner_engine():
    """Return a fresh async engine using the owner DSN (bypasses RLS)."""
    return create_async_engine(get_settings().database_owner_url, future=True)


async def auto_close_delivered_orders(now: datetime | None = None) -> int:
    """Close DELIVERED orders that have been sitting for >= 14 days.

    Returns the number of orders closed. Uses a Postgres advisory lock so
    concurrent workers never double-close.
    """
    current = now or datetime.now(UTC)
    cutoff = current - timedelta(days=AUTO_CLOSE_AFTER_DAYS)

    engine = _owner_engine()
    try:
        async with engine.begin() as conn:
            got_lock = (
                await conn.execute(
                    text("SELECT pg_try_advisory_lock(:id)"),
                    {"id": AUTO_CLOSE_LOCK_ID},
                )
            ).scalar()
            if not got_lock:
                log.info("periodic.auto_close.skipped", reason="lock held")
                return 0

            try:
                sm = async_sessionmaker(bind=conn, expire_on_commit=False)
                async with sm() as session:
                    rows = (
                        (
                            await session.execute(
                                select(Order).where(
                                    Order.status == OrderStatus.DELIVERED,
                                    Order.updated_at <= cutoff,
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    for order in rows:
                        order.status = OrderStatus.CLOSED
                        order.closed_at = current
                        session.add(
                            OrderStatusHistory(
                                tenant_id=order.tenant_id,
                                order_id=order.id,
                                from_status=OrderStatus.DELIVERED,
                                to_status=OrderStatus.CLOSED,
                                note="auto-closed after 14 days",
                            )
                        )
                    await session.flush()
                    log.info("periodic.auto_close.done", closed=len(rows))
                    return len(rows)
            finally:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(:id)"),
                    {"id": AUTO_CLOSE_LOCK_ID},
                )
    finally:
        await engine.dispose()
