"""Periodic background jobs driven by APScheduler.

Each job opens a fresh owner-scoped engine so it sees data across all
tenants (Postgres RLS policies only apply to the non-owner `portal_app`
role). A `pg_try_advisory_lock` wraps every job so running multiple
web workers won't cause the same job to execute twice.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.logging import get_logger
from app.models.customer import CustomerContact
from app.models.enums import OrderStatus
from app.models.order import Order, OrderStatusHistory

log = get_logger("app.tasks.periodic")

AUTO_CLOSE_LOCK_ID = 42_001
AUTO_CLOSE_AFTER_DAYS = 14

INVITE_CLEANUP_LOCK_ID = 42_002
INVITE_EXPIRY_DAYS = 14


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


async def cleanup_stale_invited_contacts(now: datetime | None = None) -> int:
    """Delete CustomerContact rows whose invite has expired without accept.

    Matches rows where `invited_at` is older than INVITE_EXPIRY_DAYS AND
    `accepted_at IS NULL`. Returns the number of rows deleted.
    """
    current = now or datetime.now(UTC)
    cutoff = current - timedelta(days=INVITE_EXPIRY_DAYS)

    engine = _owner_engine()
    try:
        async with engine.begin() as conn:
            got_lock = (
                await conn.execute(
                    text("SELECT pg_try_advisory_lock(:id)"),
                    {"id": INVITE_CLEANUP_LOCK_ID},
                )
            ).scalar()
            if not got_lock:
                log.info("periodic.cleanup_invites.skipped", reason="lock held")
                return 0

            try:
                result = await conn.execute(
                    delete(CustomerContact).where(
                        CustomerContact.invited_at.is_not(None),
                        CustomerContact.accepted_at.is_(None),
                        CustomerContact.invited_at <= cutoff,
                    )
                )
                removed = result.rowcount or 0
                log.info("periodic.cleanup_invites.done", removed=removed)
                return removed
            finally:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(:id)"),
                    {"id": INVITE_CLEANUP_LOCK_ID},
                )
    finally:
        await engine.dispose()
