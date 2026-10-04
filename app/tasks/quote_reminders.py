"""Quote follow-up reminders (audit 2026-10-03 IDEA-2).

An order sitting in QUOTED is revenue waiting on the customer. Until now
the only signal they got was the one status email; the supplier chased
the rest by phone. Once a quote has been unanswered for
``QUOTE_REMINDER_DAYS`` (default 3, ``0`` = off) the customer's contacts
get exactly **one** reminder per quote.

Idempotency: ``orders.quote_reminder_sent_at`` is stamped when the
reminder goes out. A reminder is due only while that stamp is NULL or
older than ``orders.quoted_at`` — so a re-quote (which re-stamps
``quoted_at``) re-arms it, and nothing else ever does.

Consent: recipients resolve through
:func:`app.services.notification_service.build_quote_reminder`, i.e. the
same ``_select`` as every other notification — a contact who switched the
event off never receives it (CLAUDE.md §19).

Runs as the owner role (bypasses RLS) like every periodic job, with a
``pg_try_advisory_lock`` so two workers cannot double-send. The tenant
row is loaded per order so links point at the right subdomain.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.logging import get_logger
from app.models.enums import OrderStatus
from app.models.order import Order

log = get_logger("app.tasks.quote_reminders")

#: Distinct from the 42_00x ids in ``app.tasks.periodic``.
QUOTE_REMINDER_LOCK_ID = 42_101

#: Safety valve per run; the next tick picks up the rest.
MAX_REMINDERS_PER_RUN = 500


async def send_quote_reminders(now: datetime | None = None, sender=None) -> int:
    """Send due quote reminders; return how many orders were reminded."""
    from app.email.sender import build_sender

    settings = get_settings()
    days = int(settings.quote_reminder_days or 0)
    if days <= 0:
        return 0

    current = now or datetime.now(UTC)
    mail = sender if sender is not None else build_sender(settings)

    engine = create_async_engine(settings.database_owner_url, future=True)
    try:
        async with engine.connect() as lock_conn:
            got_lock = (
                await lock_conn.execute(
                    text("SELECT pg_try_advisory_lock(:id)"), {"id": QUOTE_REMINDER_LOCK_ID}
                )
            ).scalar()
            if not got_lock:
                log.info("periodic.quote_reminders.skipped", reason="lock held")
                return 0
            try:
                sent = await _run(engine, current, days, mail, settings)
            finally:
                await lock_conn.execute(
                    text("SELECT pg_advisory_unlock(:id)"), {"id": QUOTE_REMINDER_LOCK_ID}
                )
        log.info("periodic.quote_reminders.done", reminded=sent)
        return sent
    finally:
        await engine.dispose()


async def _run(engine, current: datetime, days: int, mail, settings) -> int:
    from app.models.tenant import Tenant
    from app.services.notification_service import build_quote_reminder
    from app.tasks.email_tasks import send_order_notifications
    from app.urls import tenant_base_url

    cutoff = current - timedelta(days=days)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    reminded = 0
    outbox: list = []
    async with sm() as session, session.begin():
        due = (
            (
                await session.execute(
                    select(Order)
                    .join(Tenant, Tenant.id == Order.tenant_id)
                    .where(
                        Tenant.is_active.is_(True),
                        Order.status == OrderStatus.QUOTED,
                        Order.quoted_at.is_not(None),
                        Order.quoted_at <= cutoff,
                        or_(
                            Order.quote_reminder_sent_at.is_(None),
                            Order.quote_reminder_sent_at < Order.quoted_at,
                        ),
                    )
                    .order_by(Order.quoted_at)
                    .limit(MAX_REMINDERS_PER_RUN)
                    .with_for_update(of=Order, skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        tenants: dict = {}
        for order in due:
            tenant = tenants.get(order.tenant_id)
            if tenant is None:
                tenant = (
                    await session.execute(select(Tenant).where(Tenant.id == order.tenant_id))
                ).scalar_one()
                tenants[order.tenant_id] = tenant
            payloads = await build_quote_reminder(
                session,
                tenant=tenant,
                order=order,
                base_url=tenant_base_url(settings, tenant),
                settings=settings,
            )
            # Stamp even when nobody consented: the decision for this
            # quote has been made, and re-evaluating it daily would mail a
            # contact the moment they re-enable the event weeks later.
            order.quote_reminder_sent_at = current
            outbox.extend(payloads)
            reminded += 1
            log.info(
                "periodic.quote_reminders.sent",
                tenant_id=str(order.tenant_id),
                order_id=str(order.id),
                recipients=len(payloads),
            )
    # Send only after the markers are committed (same rule as CLAUDE.md
    # §2): a failed commit must not leave customers mailed but unmarked,
    # or tomorrow's run would mail them again. SMTP is blocking, so keep
    # it off the event loop.
    if outbox:
        await asyncio.to_thread(send_order_notifications, mail, outbox)
    return reminded
