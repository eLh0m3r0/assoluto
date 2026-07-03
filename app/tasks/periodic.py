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
from app.models.order import Order, OrderComment, OrderStatusHistory

log = get_logger("app.tasks.periodic")

AUTO_CLOSE_LOCK_ID = 42_001
AUTO_CLOSE_AFTER_DAYS = 14

INVITE_CLEANUP_LOCK_ID = 42_002
INVITE_EXPIRY_DAYS = 14

STRIPE_EVENT_CLEANUP_LOCK_ID = 42_003
# Stripe retries failed webhook deliveries for ~3 days. We keep 30 days
# for audit purposes, then prune — the dedup table would otherwise grow
# unbounded at ~100 events / tenant / month. Round-2 audit S-N8.
STRIPE_EVENT_RETENTION_DAYS = 30

# 42_005 is reserved by `_sync_stripe_prices_from_env` in app.main —
# reusing it caused one of the two jobs to silently no-op when both
# tried to grab the lock in the same boot window.
EXPIRE_TRIALS_LOCK_ID = 42_006

# Grace period (in days) the tenant keeps full access AFTER the paid
# subscription period ends, so they can export their data. After that,
# ``enforce_canceled_subscriptions`` deactivates the tenant. Marketing
# (pricing FAQ + index FAQ) commits us to this number — keep them in sync.
ENFORCE_CANCELED_LOCK_ID = 42_007
CANCEL_GRACE_DAYS = 3


def _owner_engine():
    """Return a fresh async engine using the owner DSN (bypasses RLS)."""
    return create_async_engine(get_settings().database_owner_url, future=True)


async def auto_close_delivered_orders(now: datetime | None = None) -> int:
    """Close DELIVERED orders that have been sitting for >= 14 days.

    Skips orders that have comments newer than the cutoff — active
    discussion means the order shouldn't be auto-closed yet.

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
                    from sqlalchemy import func as sa_func

                    latest_comment = (
                        select(
                            OrderComment.order_id,
                            sa_func.max(OrderComment.created_at).label("last_comment_at"),
                        )
                        .group_by(OrderComment.order_id)
                        .subquery()
                    )

                    stmt = (
                        select(Order)
                        .outerjoin(latest_comment, Order.id == latest_comment.c.order_id)
                        .where(
                            Order.status == OrderStatus.DELIVERED,
                            Order.updated_at <= cutoff,
                            (
                                (latest_comment.c.last_comment_at.is_(None))
                                | (latest_comment.c.last_comment_at <= cutoff)
                            ),
                        )
                    )
                    rows = (await session.execute(stmt)).scalars().all()
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


async def cleanup_old_stripe_events(now: datetime | None = None) -> int:
    """Prune ``platform_stripe_events`` rows older than the retention
    window. Stripe's own webhook retry window is ~72 h; we keep 30 d for
    audit + debugging. Returns the number of rows deleted."""
    current = now or datetime.now(UTC)
    cutoff = current - timedelta(days=STRIPE_EVENT_RETENTION_DAYS)

    engine = _owner_engine()
    try:
        async with engine.begin() as conn:
            got_lock = (
                await conn.execute(
                    text("SELECT pg_try_advisory_lock(:id)"),
                    {"id": STRIPE_EVENT_CLEANUP_LOCK_ID},
                )
            ).scalar()
            if not got_lock:
                log.info("periodic.stripe_events.skipped", reason="lock held")
                return 0
            try:
                result = await conn.execute(
                    text("DELETE FROM platform_stripe_events WHERE received_at <= :cutoff"),
                    {"cutoff": cutoff},
                )
                removed = result.rowcount or 0
                log.info("periodic.stripe_events.done", removed=removed)
                return removed
            finally:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(:id)"),
                    {"id": STRIPE_EVENT_CLEANUP_LOCK_ID},
                )
    finally:
        await engine.dispose()


async def expire_demo_trials(now: datetime | None = None) -> int:
    """Mark expired demo-mode trials as canceled.

    In live Stripe mode, ``customer.subscription.deleted`` does the
    same thing via webhook. This job is defence-in-depth for demo/dev
    mode where no webhook fires.

    Status flips to ``canceled``; ``plan_id`` is intentionally left as
    a record of what the tenant had on trial. Per Option A there is no
    free hosted "Community" fallback — once canceled, the tenant has
    ``CANCEL_GRACE_DAYS`` days from ``current_period_end`` (or now,
    whichever is later) before ``enforce_canceled_subscriptions``
    deactivates them.
    """
    current = now or datetime.now(UTC)

    engine = _owner_engine()
    try:
        async with engine.begin() as conn:
            got_lock = (
                await conn.execute(
                    text("SELECT pg_try_advisory_lock(:id)"),
                    {"id": EXPIRE_TRIALS_LOCK_ID},
                )
            ).scalar()
            if not got_lock:
                log.info("periodic.expire_trials.skipped", reason="lock held")
                return 0

            try:
                # Stamp current_period_end at the trial-end so
                # enforce_canceled_subscriptions has a clear grace anchor.
                # COALESCE protects the rare row where the field was already
                # set further out (e.g. operator extended trial manually).
                result = await conn.execute(
                    text(
                        "UPDATE platform_subscriptions "
                        "SET status = 'canceled', "
                        "    current_period_end = COALESCE(current_period_end, trial_ends_at) "
                        "WHERE status IN ('trialing', 'demo') "
                        "  AND trial_ends_at IS NOT NULL "
                        "  AND trial_ends_at < :now "
                        "  AND stripe_subscription_id IS NULL"
                    ),
                    {"now": current},
                )
                expired = result.rowcount or 0
                log.info("periodic.expire_trials.done", expired=expired)
                return expired
            finally:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(:id)"),
                    {"id": EXPIRE_TRIALS_LOCK_ID},
                )
    finally:
        await engine.dispose()


async def enforce_canceled_subscriptions(now: datetime | None = None) -> int:
    """Deactivate tenants whose canceled subscription has run out of grace.

    A canceled subscription gets ``CANCEL_GRACE_DAYS`` (today: 3) of
    full access AFTER its ``current_period_end`` so the operator can
    export their data. This job runs daily and, for every canceled
    subscription where the grace window has expired, flips
    ``tenants.is_active = false`` and bumps every user/contact's
    ``session_version`` so any in-flight browser session fails its
    next request.

    Tenants with ``is_active=false`` are returned as 404 by
    ``deps.get_current_tenant`` — no further code path needs to be
    aware of the cancellation.

    Idempotent: re-running on already-deactivated tenants is a no-op.
    """
    current = now or datetime.now(UTC)
    cutoff = current - timedelta(days=CANCEL_GRACE_DAYS)

    engine = _owner_engine()
    try:
        async with engine.begin() as conn:
            got_lock = (
                await conn.execute(
                    text("SELECT pg_try_advisory_lock(:id)"),
                    {"id": ENFORCE_CANCELED_LOCK_ID},
                )
            ).scalar()
            if not got_lock:
                log.info("periodic.enforce_canceled.skipped", reason="lock held")
                return 0

            try:
                # Find tenants whose subscription is canceled and whose
                # grace window has elapsed AND which are still active
                # (so re-runs don't re-disable already-disabled rows).
                rows = (
                    await conn.execute(
                        text(
                            "SELECT t.id "
                            "FROM tenants t "
                            "JOIN platform_subscriptions s ON s.tenant_id = t.id "
                            "WHERE s.status = 'canceled' "
                            "  AND s.current_period_end IS NOT NULL "
                            "  AND s.current_period_end < :cutoff "
                            "  AND t.is_active = true"
                        ),
                        {"cutoff": cutoff},
                    )
                ).all()

                deactivated = 0
                for (tenant_id,) in rows:
                    await conn.execute(
                        text("UPDATE tenants SET is_active = false WHERE id = :id"),
                        {"id": tenant_id},
                    )
                    # Mirror deactivate_tenant: bump session_version on
                    # every user + customer_contact so existing browser
                    # cookies fail their next request.
                    await conn.execute(
                        text(
                            "UPDATE users "
                            "SET session_version = session_version + 1 "
                            "WHERE tenant_id = :tid"
                        ),
                        {"tid": tenant_id},
                    )
                    await conn.execute(
                        text(
                            "UPDATE customer_contacts "
                            "SET session_version = session_version + 1 "
                            "WHERE tenant_id = :tid"
                        ),
                        {"tid": tenant_id},
                    )
                    deactivated += 1
                    log.info("periodic.enforce_canceled.tenant_disabled", tenant_id=str(tenant_id))

                log.info("periodic.enforce_canceled.done", deactivated=deactivated)
                return deactivated
            finally:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(:id)"),
                    {"id": ENFORCE_CANCELED_LOCK_ID},
                )
    finally:
        await engine.dispose()


# Trial-nurture cadence. Each nudge has a send *window* so enabling the
# feature on an install with existing trials doesn't blast every tenant
# with all three emails at once — a nudge whose window already passed is
# silently skipped.
TRIAL_NURTURE_LOCK_ID = 42_008
NURTURE_WINDOW_DAYS = 3
TRIAL_ENDING_LEAD_DAYS = 5
# Marker key inside tenants.settings: {"day1": "<iso>", ...}. Underscore
# prefix = machine-managed, mirrors the "_gdpr_erased_at" convention.
NURTURE_SENT_KEY = "_trial_nurture_sent"


def _tenant_portal_url(base_url: str, slug: str) -> str:
    """Derive the tenant's subdomain URL from APP_BASE_URL."""
    from urllib.parse import urlsplit

    parts = urlsplit(base_url)
    return f"{parts.scheme}://{slug}.{parts.netloc}"


def _due_nurture_stage(
    now: datetime,
    created_at: datetime,
    trial_ends_at: datetime | None,
    already_sent: dict,
) -> tuple[str, int] | None:
    """Return the most urgent unsent stage whose window covers ``now``.

    Priority: ending > day7 > day1 (at most one email per tenant per
    run, so overlapping windows on short trials can't double-send).
    """
    window = timedelta(days=NURTURE_WINDOW_DAYS)
    if (
        trial_ends_at is not None
        and "ending" not in already_sent
        and trial_ends_at - timedelta(days=TRIAL_ENDING_LEAD_DAYS) <= now < trial_ends_at
    ):
        return "ending", max(0, (trial_ends_at - now).days)
    if trial_ends_at is not None and now >= trial_ends_at:
        return None  # trial over — expiry job owns it from here
    for stage, offset in (("day7", timedelta(days=7)), ("day1", timedelta(days=1))):
        start = created_at + offset
        if stage not in already_sent and start <= now < start + window:
            return stage, 0
    return None


async def send_trial_nurture_emails(now: datetime | None = None, sender=None) -> int:
    """Send the day-1 / day-7 / trial-ending nudges to trial tenants.

    Recipients are the tenant's active TENANT_ADMIN users. Sent stages
    are recorded in ``tenants.settings["_trial_nurture_sent"]`` so the
    job is idempotent across daily runs. Gated on both FEATURE_PLATFORM
    and TRIAL_NURTURE_ENABLED (default off — copy must be approved
    before any tenant receives it).

    ``platform_subscriptions`` is queried via raw SQL on purpose: core
    tasks must not import ``app.platform`` models (CLAUDE.md §6).
    """
    from app.email.sender import build_sender

    settings = get_settings()
    if not (settings.feature_platform and settings.trial_nurture_enabled):
        return 0

    current = now or datetime.now(UTC)
    mail = sender if sender is not None else build_sender(settings)

    engine = _owner_engine()
    try:
        # The advisory lock is session-level: it survives transactions on
        # `lock_conn` and is held until the explicit unlock below, so the
        # whole send loop is covered — two workers ticking simultaneously
        # can't double-send.
        async with engine.connect() as lock_conn:
            got_lock = (
                await lock_conn.execute(
                    text("SELECT pg_try_advisory_lock(:id)"),
                    {"id": TRIAL_NURTURE_LOCK_ID},
                )
            ).scalar()
            if not got_lock:
                log.info("periodic.trial_nurture.skipped", reason="lock held")
                return 0
            try:
                sent = await _run_trial_nurture(engine, current, mail, settings)
            finally:
                await lock_conn.execute(
                    text("SELECT pg_advisory_unlock(:id)"),
                    {"id": TRIAL_NURTURE_LOCK_ID},
                )
        log.info("periodic.trial_nurture.done", sent=sent)
        return sent
    finally:
        await engine.dispose()


async def _run_trial_nurture(engine, current: datetime, mail, settings) -> int:
    """Inner body of :func:`send_trial_nurture_emails` (lock already held)."""
    from app.models.enums import UserRole
    from app.models.tenant import Tenant
    from app.models.user import User as UserModel
    from app.services.locale_service import resolve_email_locale
    from app.tasks.email_tasks import send_trial_nurture

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT s.tenant_id, s.created_at, s.trial_ends_at "
                    "FROM platform_subscriptions s "
                    "JOIN tenants t ON t.id = s.tenant_id "
                    "WHERE s.status IN ('trialing', 'demo') "
                    "  AND t.is_active = true"
                )
            )
        ).all()

    sent = 0
    sm = async_sessionmaker(engine, expire_on_commit=False)
    for tenant_id, created_at, trial_ends_at in rows:
        async with sm() as session, session.begin():
            tenant = (
                await session.execute(select(Tenant).where(Tenant.id == tenant_id))
            ).scalar_one()
            already = dict((tenant.settings or {}).get(NURTURE_SENT_KEY) or {})
            due = _due_nurture_stage(current, created_at, trial_ends_at, already)
            if due is None:
                continue
            stage, days_left = due

            admins = (
                (
                    await session.execute(
                        select(UserModel).where(
                            UserModel.tenant_id == tenant_id,
                            UserModel.role == UserRole.TENANT_ADMIN,
                            UserModel.is_active.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not admins:
                continue

            portal_url = _tenant_portal_url(settings.app_base_url, tenant.slug)
            billing_url = settings.app_base_url.rstrip("/") + "/platform/billing"
            trial_end_date = trial_ends_at.strftime("%d.%m.%Y") if trial_ends_at is not None else ""
            for admin in admins:
                locale = resolve_email_locale(recipient=admin, tenant=tenant, settings=settings)
                send_trial_nurture(
                    mail,
                    to=admin.email,
                    stage=stage,
                    full_name=admin.full_name,
                    tenant_name=tenant.name,
                    portal_url=portal_url,
                    billing_url=billing_url,
                    trial_end_date=trial_end_date,
                    days_left=days_left,
                    locale=locale,
                )
                sent += 1

            already[stage] = current.isoformat()
            tenant.settings = {**(tenant.settings or {}), NURTURE_SENT_KEY: already}
            log.info(
                "periodic.trial_nurture.sent",
                tenant_id=str(tenant_id),
                stage=stage,
                recipients=len(admins),
            )

    return sent
