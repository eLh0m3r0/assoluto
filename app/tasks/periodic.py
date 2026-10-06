"""Periodic background jobs driven by APScheduler.

Each job opens a fresh owner-scoped engine so it sees data across all
tenants (Postgres RLS policies only apply to the non-owner `portal_app`
role). A `pg_try_advisory_lock` wraps every job so running multiple
web workers won't cause the same job to execute twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import Uuid, delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.logging import get_logger
from app.models.customer import CustomerContact
from app.models.enums import OrderStatus
from app.models.order import Order, OrderComment, OrderStatusHistory
from app.services.early_access import (
    COVERED_BY_EARLY_ACCESS_SQL,
    EARLY_ACCESS_TZ,
    EFFECTIVE_TRIAL_END_SQL,
    early_access_ends_at,
)

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
# Codex-8 entitlement grace windows for the other non-paying Stripe
# states (see ``access_cutoff``). ``past_due`` keeps access while
# Stripe's Smart Retries run (default schedule ≈ 3 weeks) — the cut
# must not land before the last retry. The no-access states get the
# same short export window as a cancellation.
PAST_DUE_GRACE_DAYS = 21
NO_ACCESS_GRACE_DAYS = CANCEL_GRACE_DAYS
NO_ACCESS_STATUSES: frozenset[str] = frozenset(
    {"unpaid", "incomplete", "incomplete_expired", "paused"}
)


def _system_actor(job: str):
    """Audit actor for a scheduled job: type ``system``, labelled with the
    job name so the tenant's audit log says *which* job acted (BE-13)."""
    from app.services.audit_service import ActorInfo

    return ActorInfo(type="system", id=None, label=f"system: {job}")


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
                    from app.services import audit_service

                    actor = _system_actor("auto_close_delivered_orders")
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
                        # Same action/shape as a manual transition so the
                        # activity feed renders it like any status change.
                        await audit_service.record(
                            session,
                            action="order.status_changed",
                            entity_type="order",
                            entity_id=order.id,
                            entity_label=order.number,
                            actor=actor,
                            before={"status": OrderStatus.DELIVERED.value},
                            after={"status": OrderStatus.CLOSED.value, "auto_closed": True},
                            tenant_id=order.tenant_id,
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
                from app.services import audit_service

                stale = (
                    await conn.execute(
                        select(
                            CustomerContact.id,
                            CustomerContact.tenant_id,
                            CustomerContact.customer_id,
                            CustomerContact.email,
                            CustomerContact.invited_at,
                        ).where(
                            CustomerContact.invited_at.is_not(None),
                            CustomerContact.accepted_at.is_(None),
                            CustomerContact.invited_at <= cutoff,
                        )
                    )
                ).all()
                if not stale:
                    log.info("periodic.cleanup_invites.done", removed=0)
                    return 0
                result = await conn.execute(
                    delete(CustomerContact).where(CustomerContact.id.in_([r.id for r in stale]))
                )
                removed = result.rowcount or 0
                # One audit row per purged invitation so the supplier can
                # see why a contact vanished from the customer card (BE-13).
                sm = async_sessionmaker(bind=conn, expire_on_commit=False)
                async with sm() as session:
                    actor = _system_actor("cleanup_stale_invited_contacts")
                    for row in stale:
                        await audit_service.record(
                            session,
                            action="contact.invite_expired",
                            entity_type="contact",
                            entity_id=row.id,
                            entity_label=row.email,
                            actor=actor,
                            after={
                                "customer_id": str(row.customer_id),
                                "invited_at": row.invited_at.isoformat(),
                                "expired_after_days": INVITE_EXPIRY_DAYS,
                            },
                            tenant_id=row.tenant_id,
                        )
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
    """Cancel local (non-Stripe) subscriptions whose paid-for time ran out.

    In live Stripe mode, ``customer.subscription.deleted`` does the
    same thing via webhook. This job covers every subscription Stripe
    does not manage:

    * ``trialing`` / ``demo`` rows past their *effective* trial end —
      ``max(trial_ends_at, early-access end)`` (E1, see
      :mod:`app.services.early_access`), so nobody is cut off before
      ``EARLY_ACCESS_UNTIL``;
    * manually invoiced ``active`` rows past their ``current_period_end``
      (LOGIC-18) — only rows written by the new subscription editor,
      which stamps ``status_changed_at`` and requires an explicit
      "paid until" date. Legacy manual rows (``status_changed_at`` NULL)
      are left alone so nobody is cut off by a date that was never
      meant as an end date; the operator re-saves them with one.

    Status flips to ``canceled``; ``plan_id`` is intentionally left as
    a record of what the tenant had. Per Option A there is no free
    hosted "Community" fallback — once canceled, the tenant has
    ``CANCEL_GRACE_DAYS`` days from ``current_period_end`` before
    ``enforce_canceled_subscriptions`` deactivates them.
    """
    current = now or datetime.now(UTC)
    early_access_end = early_access_ends_at(get_settings().early_access_until)

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
                # A trial that ran on early access gets the early-access
                # end as its anchor (GREATEST ignores the NULL for every
                # other row) — otherwise the 3-day export window would be
                # measured from a trial date months in the past.
                result = await conn.execute(
                    text(
                        "UPDATE platform_subscriptions AS s "
                        "SET status = 'canceled', "
                        "    status_changed_at = :now, "
                        "    canceled_at = COALESCE(s.canceled_at, :now), "
                        "    current_period_end = GREATEST("
                        "      COALESCE(s.current_period_end, s.trial_ends_at), "
                        f"      CASE WHEN {COVERED_BY_EARLY_ACCESS_SQL} "
                        "           THEN CAST(:early_access_end AS timestamptz) END) "
                        "WHERE s.stripe_subscription_id IS NULL "
                        "  AND ( "
                        "    (s.status IN ('trialing', 'demo') "
                        "     AND s.trial_ends_at IS NOT NULL "
                        f"     AND {EFFECTIVE_TRIAL_END_SQL} < :now) "
                        "    OR (s.status = 'active' "
                        "     AND s.status_changed_at IS NOT NULL "
                        "     AND s.current_period_end IS NOT NULL "
                        "     AND s.current_period_end < :now) "
                        "  ) "
                        "RETURNING s.id, s.tenant_id"
                    ),
                    {"now": current, "early_access_end": early_access_end},
                )
                expired_rows = result.all()
                for sub_id, tenant_id in expired_rows:
                    await _billing_job_audit(
                        conn,
                        job="expire_demo_trials",
                        action="billing.subscription_expired",
                        entity_type="subscription",
                        entity_id=sub_id,
                        tenant_id=tenant_id,
                        after={"status": "canceled", "at": current.isoformat()},
                    )
                expired = len(expired_rows)
                log.info("periodic.expire_trials.done", expired=expired)
                return expired
            finally:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(:id)"),
                    {"id": EXPIRE_TRIALS_LOCK_ID},
                )
    finally:
        await engine.dispose()


async def _billing_job_audit(
    conn,
    *,
    job: str,
    action: str,
    entity_type: str,
    entity_id,
    tenant_id,
    after: dict,
) -> None:
    """Tenant-visible audit row for a billing job's decision (BE-13).

    Scheduled jobs disabled tenants without a trace; the tenant admin's
    audit log now says which job did what. Actor = ``system`` /
    ``job:<name>``. Written in the job's own transaction.
    """
    from app.services import audit_service
    from app.services.audit_service import ActorInfo

    sm = async_sessionmaker(bind=conn, expire_on_commit=False)
    async with sm() as session:
        await audit_service.record(
            session,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_label=job,
            actor=ActorInfo(type="system", id=None, label=f"job:{job}"),
            after=after,
            tenant_id=tenant_id,
        )
        await session.flush()


def access_cutoff(
    status: str | None,
    *,
    current_period_end: datetime | None,
    status_changed_at: datetime | None,
    canceled_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> datetime | None:
    """When does a tenant in ``status`` lose access? ``None`` = not scheduled.

    Single source for the entitlement table in
    ``app.platform.billing.service`` (Codex-8). Used by the in-app
    banners; ``enforce_canceled_subscriptions`` applies the same rules
    in SQL (see ``_CUTOFF_SQL``).
    """
    if status == "canceled":
        anchor = current_period_end or canceled_at or status_changed_at or updated_at
        return anchor + timedelta(days=CANCEL_GRACE_DAYS) if anchor else None
    anchor = status_changed_at or updated_at
    if anchor is None:
        return None
    if status == "past_due":
        return anchor + timedelta(days=PAST_DUE_GRACE_DAYS)
    if status in NO_ACCESS_STATUSES:
        return anchor + timedelta(days=NO_ACCESS_GRACE_DAYS)
    return None


# SQL twin of :func:`access_cutoff` — "the grace window has elapsed".
_CUTOFF_SQL = (
    "("
    "  (s.status = 'canceled' "
    "   AND COALESCE(s.current_period_end, s.canceled_at, s.status_changed_at, s.updated_at)"
    "       < :canceled_cutoff) "
    "  OR (s.status = 'past_due' "
    "   AND COALESCE(s.status_changed_at, s.updated_at) < :past_due_cutoff) "
    "  OR (s.status IN ('unpaid', 'incomplete', 'incomplete_expired', 'paused') "
    "   AND COALESCE(s.status_changed_at, s.updated_at) < :no_access_cutoff) "
    ")"
)


async def enforce_canceled_subscriptions(now: datetime | None = None) -> int:
    """Deactivate tenants whose non-paying subscription ran out of grace.

    Entitlement rules (Codex-8, see :func:`access_cutoff`):

    * ``canceled`` — ``CANCEL_GRACE_DAYS`` after ``current_period_end``
      so the operator can export their data;
    * ``past_due`` — ``PAST_DUE_GRACE_DAYS`` after the status changed,
      i.e. after Stripe's dunning retries had their chance;
    * ``unpaid`` / ``incomplete`` / ``incomplete_expired`` / ``paused`` —
      ``NO_ACCESS_GRACE_DAYS`` after the status changed. These used to be
      stored verbatim and never acted on, so such tenants kept access
      forever.

    For every match it flips ``tenants.is_active = false`` and bumps every
    user/contact's ``session_version`` so any in-flight browser session
    fails its next request. Tenants with ``is_active=false`` see the
    neutral "portal temporarily unavailable" page.

    A later Stripe recovery (``active``) re-enables the tenant via the
    webhook, unless a platform admin suspended it.

    Idempotent: re-running on already-deactivated tenants is a no-op.
    """
    current = now or datetime.now(UTC)
    params = {
        "canceled_cutoff": current - timedelta(days=CANCEL_GRACE_DAYS),
        "past_due_cutoff": current - timedelta(days=PAST_DUE_GRACE_DAYS),
        "no_access_cutoff": current - timedelta(days=NO_ACCESS_GRACE_DAYS),
    }

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
                # Find tenants whose grace window has elapsed AND which are
                # still active (so re-runs don't re-disable them).
                rows = (
                    await conn.execute(
                        text(
                            "SELECT t.id, s.status "
                            "FROM tenants t "
                            "JOIN platform_subscriptions s ON s.tenant_id = t.id "
                            f"WHERE {_CUTOFF_SQL} "
                            "  AND t.is_active = true"
                        ),
                        params,
                    )
                ).all()

                deactivated = 0
                for tenant_id, sub_status in rows:
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
                    await _billing_job_audit(
                        conn,
                        job="enforce_canceled_subscriptions",
                        action="tenant.deactivated_for_billing",
                        entity_type="tenant",
                        entity_id=tenant_id,
                        tenant_id=tenant_id,
                        after={"subscription_status": sub_status},
                    )
                    deactivated += 1
                    log.info(
                        "periodic.enforce_canceled.tenant_disabled",
                        tenant_id=str(tenant_id),
                        subscription_status=sub_status,
                    )

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
# Trial-ending reminders (E1): one 14 days before the *effective* trial
# end (stage ``ending14``), one 3 days before (stage ``ending``). Counted
# in Prague calendar days, so "14 days" is the date two weeks earlier
# whatever hour the job runs.
TRIAL_ENDING_LEAD_DAYS = 3
TRIAL_ENDING_EARLY_LEAD_DAYS = 14
#: Inside the nurture marker: {"ending14": "<effective end iso>", ...} —
#: the end date each ending reminder was sent for, so a later end
#: (early access, an operator extension) earns its own reminders.
ENDING_FOR_KEY = "_ending_for"
# Marker key inside tenants.settings: {"day1": "<iso>", ...}. Underscore
# prefix = machine-managed, mirrors the "_gdpr_erased_at" convention.
NURTURE_SENT_KEY = "_trial_nurture_sent"

# Behaviour-based activation nudges (BIZ-16). Unlike day1/day7 they are
# only sent when the tenant has *not* done the thing yet, so an admin who
# already invited a customer never hears "invite your first customer".
ACTIVATION_INVITE_DAY = 2  # no customer contact invited yet
ACTIVATION_NO_LOGIN_DAY = 5  # contacts invited, none has signed in

# Stage → template mapping lives next to the sender:
# ``app.tasks.email_tasks.NURTURE_TEMPLATES``.


@dataclass
class TenantActivationState:
    """What the tenant has done so far — drives the behavioural nudges."""

    contacts_invited: int = 0
    contacts_logged_in: int = 0
    contact_orders: int = 0
    #: Up to five invited-but-never-signed-in contacts, for the "resend
    #: the invitation" email.
    pending: list[dict] = field(default_factory=list)


def _tenant_portal_url(base_url: str, slug: str) -> str:
    """Derive the tenant's subdomain URL from APP_BASE_URL."""
    from urllib.parse import urlsplit

    parts = urlsplit(base_url)
    return f"{parts.scheme}://{slug}.{parts.netloc}"


def _calendar_days_left(now: datetime, end: datetime) -> int:
    """Whole calendar days from ``now`` to ``end`` in Europe/Prague."""
    return (end.astimezone(EARLY_ACCESS_TZ).date() - now.astimezone(EARLY_ACCESS_TZ).date()).days


def _ending_already_sent(already_sent: dict, stage: str, trial_ends_at: datetime) -> bool:
    """Was the ``stage`` reminder already sent for THIS end date?

    Markers written since E1 record the end date they were sent for. An
    older marker (no end date) counts only when it was sent inside this
    end's reminder period — a reminder for a 30-day trial end that early
    access has since pushed out must not swallow the new reminders.
    """
    if stage not in already_sent:
        return False
    sent_for = (already_sent.get(ENDING_FOR_KEY) or {}).get(stage)
    try:
        if sent_for is not None:
            return datetime.fromisoformat(sent_for) == trial_ends_at
        sent_at = datetime.fromisoformat(already_sent[stage])
    except (TypeError, ValueError):
        return True  # unreadable marker — never risk a double send
    return sent_at >= trial_ends_at - timedelta(days=TRIAL_ENDING_EARLY_LEAD_DAYS + 1)


def _due_nurture_stage(
    now: datetime,
    created_at: datetime,
    trial_ends_at: datetime | None,
    already_sent: dict,
    activation: TenantActivationState | None = None,
    *,
    trial_stages: bool = True,
    activation_stages: bool = False,
    ending_stage: bool = True,
) -> tuple[str, int] | None:
    """Return the most urgent unsent stage whose window covers ``now``.

    Priority: ending > ending14 > day7 > no_login > invite > day1 (at
    most one email per tenant per run, so overlapping windows can't
    double-send).

    ``trial_ends_at`` is the *effective* trial end (early access
    included). ``trial_stages`` / ``activation_stages`` mirror the two
    copy-approval flags (``TRIAL_NURTURE_ENABLED`` /
    ``ACTIVATION_NUDGES_ENABLED``); a stage family whose flag is off is
    never due. ``ending_stage`` is separate on purpose (LOGIC-4): the
    trial-ending reminders do not wait for copy approval — a trial must
    never end silently — and are off only for Stripe-linked trials,
    which convert automatically.
    """
    window = timedelta(days=NURTURE_WINDOW_DAYS)
    state = activation or TenantActivationState()
    if ending_stage and trial_ends_at is not None and now < trial_ends_at:
        days_left = max(0, _calendar_days_left(now, trial_ends_at))
        if days_left <= TRIAL_ENDING_LEAD_DAYS:
            if not _ending_already_sent(already_sent, "ending", trial_ends_at):
                return "ending", days_left
        elif days_left <= TRIAL_ENDING_EARLY_LEAD_DAYS and not any(
            # A pre-E1 "ending" mail (5-day lead) already covered this end.
            _ending_already_sent(already_sent, stage, trial_ends_at)
            for stage in ("ending14", "ending")
        ):
            return "ending14", days_left
    if trial_ends_at is not None and now >= trial_ends_at:
        return None  # trial over — expiry job owns it from here

    def _in_window(stage: str, offset_days: int) -> bool:
        start = created_at + timedelta(days=offset_days)
        return stage not in already_sent and start <= now < start + window

    # day7 says "if a client is already placing orders — ignore this";
    # when one is, don't send it at all.
    if trial_stages and state.contact_orders == 0 and _in_window("day7", 7):
        return "day7", 0
    if activation_stages:
        if (
            state.contacts_invited > 0
            and state.contacts_logged_in == 0
            and _in_window("no_login", ACTIVATION_NO_LOGIN_DAY)
        ):
            return "no_login", 0
        if state.contacts_invited == 0 and _in_window("invite", ACTIVATION_INVITE_DAY):
            return "invite", 0
    if trial_stages and _in_window("day1", 1):
        return "day1", 0
    return None


async def send_trial_nurture_emails(now: datetime | None = None, sender=None) -> int:
    """Send the trial nudges and the behaviour-based activation nudges.

    Time-based onboarding stages (day-1 / day-7) are gated on
    ``TRIAL_NURTURE_ENABLED``; the activation stages ("invite your first
    customer" on day 2 when nobody was invited, "your customer hasn't
    signed in — resend the invite" on day 5 when invited contacts never
    signed in) on ``ACTIVATION_NUDGES_ENABLED``. Both default off — copy
    must be approved first. The trial-ENDING reminder waits for neither
    (LOGIC-4): a trial must never end silently and three days later cut
    the tenant — and its customers — off without a warning. It is skipped
    for Stripe-linked trials, which convert automatically. All of it
    needs FEATURE_PLATFORM.

    Recipients are the tenant's active TENANT_ADMIN users, only for
    tenants whose signup email was verified, never an address of a
    still-unverified identity (BIZ-09) and never an operator's
    support-access user (LOGIC-20). Sent stages are recorded in
    ``tenants.settings["_trial_nurture_sent"]`` so the job is idempotent
    across daily runs.

    ``platform_subscriptions`` / ``platform_identities`` are queried via
    raw SQL on purpose: core tasks must not import ``app.platform``
    models (CLAUDE.md §6).
    """
    from app.email.sender import build_sender

    settings = get_settings()
    trial_on = bool(settings.trial_nurture_enabled)
    activation_on = bool(settings.activation_nudges_enabled)
    if not settings.feature_platform:
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
                sent = await _run_trial_nurture(
                    engine,
                    current,
                    mail,
                    settings,
                    trial_on=trial_on,
                    activation_on=activation_on,
                )
            finally:
                await lock_conn.execute(
                    text("SELECT pg_advisory_unlock(:id)"),
                    {"id": TRIAL_NURTURE_LOCK_ID},
                )
        log.info("periodic.trial_nurture.done", sent=sent)
        return sent
    finally:
        await engine.dispose()


# A tenant is nurtured only when its signup identity verified the email.
# Tenants with no member identity at all (created by scripts/create_tenant
# before the platform layer existed) have nobody to verify and stay
# eligible.
# The trial end is the *effective* one (E1, SQL twin of
# app.services.early_access) — bind ``:early_access_end``.
_NURTURE_TENANTS_SQL = text(
    "SELECT s.tenant_id, s.created_at, "
    f"  {EFFECTIVE_TRIAL_END_SQL} AS trial_ends_at, "
    "   s.stripe_subscription_id, "
    f"  {COVERED_BY_EARLY_ACCESS_SQL} AS early_access "
    "FROM platform_subscriptions s "
    "JOIN tenants t ON t.id = s.tenant_id "
    "WHERE s.status IN ('trialing', 'demo') "
    "  AND t.is_active = true "
    "  AND ("
    "    EXISTS (SELECT 1 FROM platform_tenant_memberships m "
    "            JOIN platform_identities i ON i.id = m.identity_id "
    "            WHERE m.tenant_id = s.tenant_id AND m.access_type = 'member' "
    "              AND i.email_verified_at IS NOT NULL) "
    "    OR NOT EXISTS (SELECT 1 FROM platform_tenant_memberships m "
    "            WHERE m.tenant_id = s.tenant_id AND m.access_type = 'member')"
    "  )"
)

# Admin addresses that belong to a still-unverified identity of this
# tenant. Never mailed, even when a verified co-owner exists.
_UNVERIFIED_EMAILS_SQL = text(
    "SELECT lower(i.email) FROM platform_tenant_memberships m "
    "JOIN platform_identities i ON i.id = m.identity_id "
    "WHERE m.tenant_id = :tid AND i.email_verified_at IS NULL"
)

# Users created by an operator's support-access grant (LOGIC-20). Raw SQL —
# core may not import the platform models (CLAUDE.md §6).
_SUPPORT_USER_IDS = text(
    "SELECT m.user_id FROM platform_tenant_memberships m "
    "WHERE m.user_id IS NOT NULL AND m.access_type = 'support'"
).columns(user_id=Uuid())

_ACTIVATION_COUNTS_SQL = text(
    "SELECT "
    " (SELECT count(*) FROM customer_contacts WHERE tenant_id = :tid), "
    " (SELECT count(*) FROM customer_contacts WHERE tenant_id = :tid "
    "    AND (last_login_at IS NOT NULL OR accepted_at IS NOT NULL)), "
    " (SELECT count(*) FROM orders WHERE tenant_id = :tid "
    "    AND created_by_contact_id IS NOT NULL)"
)

_PENDING_CONTACTS_SQL = text(
    "SELECT cc.full_name, c.name, c.id FROM customer_contacts cc "
    "JOIN customers c ON c.id = cc.customer_id "
    "WHERE cc.tenant_id = :tid AND cc.is_active = true "
    "  AND cc.accepted_at IS NULL AND cc.last_login_at IS NULL "
    "ORDER BY cc.invited_at NULLS LAST LIMIT 5"
)


async def _activation_state(session, tenant_id) -> TenantActivationState:
    """Count what the tenant has done (owner session — sees every tenant)."""
    counts = (await session.execute(_ACTIVATION_COUNTS_SQL, {"tid": tenant_id})).one()
    pending_rows = (await session.execute(_PENDING_CONTACTS_SQL, {"tid": tenant_id})).all()
    return TenantActivationState(
        contacts_invited=int(counts[0]),
        contacts_logged_in=int(counts[1]),
        contact_orders=int(counts[2]),
        pending=[
            {"contact_name": name, "customer_name": cname, "customer_id": str(cid)}
            for name, cname, cid in pending_rows
        ],
    )


async def _run_trial_nurture(
    engine,
    current: datetime,
    mail,
    settings,
    *,
    trial_on: bool = True,
    activation_on: bool = False,
) -> int:
    """Inner body of :func:`send_trial_nurture_emails` (lock already held)."""
    from app.models.enums import UserRole
    from app.models.tenant import Tenant
    from app.models.user import User as UserModel
    from app.services.locale_service import resolve_email_locale
    from app.tasks.email_tasks import send_trial_nurture

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                _NURTURE_TENANTS_SQL,
                {"early_access_end": early_access_ends_at(settings.early_access_until)},
            )
        ).all()

    sent = 0
    sm = async_sessionmaker(engine, expire_on_commit=False)
    for tenant_id, created_at, trial_ends_at, stripe_sub_id, early_access in rows:
        async with sm() as session, session.begin():
            tenant = (
                await session.execute(select(Tenant).where(Tenant.id == tenant_id))
            ).scalar_one()
            already = dict((tenant.settings or {}).get(NURTURE_SENT_KEY) or {})
            activation = await _activation_state(session, tenant_id)
            due = _due_nurture_stage(
                current,
                created_at,
                trial_ends_at,
                already,
                activation,
                trial_stages=trial_on,
                activation_stages=activation_on,
                ending_stage=stripe_sub_id is None,
            )
            if due is None:
                continue
            stage, days_left = due

            unverified = {
                row[0]
                for row in (await session.execute(_UNVERIFIED_EMAILS_SQL, {"tid": tenant_id})).all()
            }
            admins = [
                admin
                for admin in (
                    await session.execute(
                        select(UserModel).where(
                            UserModel.tenant_id == tenant_id,
                            UserModel.role == UserRole.TENANT_ADMIN,
                            UserModel.is_active.is_(True),
                            UserModel.id.not_in(_SUPPORT_USER_IDS),
                        )
                    )
                )
                .scalars()
                .all()
                if admin.email.strip().lower() not in unverified
            ]
            if not admins:
                continue

            portal_url = _tenant_portal_url(settings.app_base_url, tenant.slug)
            billing_url = (
                settings.app_base_url.rstrip("/") + f"/platform/billing?tenant={tenant.slug}"
            )
            trial_end_date = (
                trial_ends_at.astimezone(EARLY_ACCESS_TZ).strftime("%d.%m.%Y")
                if trial_ends_at is not None
                else ""
            )
            pending = [
                {**p, "url": f"{portal_url}/app/customers/{p['customer_id']}"}
                for p in activation.pending
            ]
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
                    pending_contacts=pending,
                    early_access=bool(early_access),
                    locale=locale,
                )
                sent += 1

            already[stage] = current.isoformat()
            if stage in ("ending", "ending14") and trial_ends_at is not None:
                already[ENDING_FOR_KEY] = {
                    **(already.get(ENDING_FOR_KEY) or {}),
                    stage: trial_ends_at.isoformat(),
                }
            tenant.settings = {**(tenant.settings or {}), NURTURE_SENT_KEY: already}
            log.info(
                "periodic.trial_nurture.sent",
                tenant_id=str(tenant_id),
                stage=stage,
                recipients=len(admins),
            )

    return sent


# Weekly open-orders summary to each opted-in customer (IDEA-10). Lock id
# picked well away from the 42_00x block so parallel additions there
# can't collide.
WEEKLY_SUMMARY_LOCK_ID = 42_102
#: tenants.settings marker: the ISO week ("2026-W41") last processed, so
#: a restart or a second worker in the same week never double-sends.
WEEKLY_SUMMARY_SENT_KEY = "_weekly_summary_sent"
#: Orders the customer is waiting on. DRAFT is the customer's own unsent
#: work; DELIVERED / CLOSED / CANCELLED are done.
WEEKLY_SUMMARY_STATUSES: tuple[OrderStatus, ...] = (
    OrderStatus.SUBMITTED,
    OrderStatus.QUOTED,
    OrderStatus.CONFIRMED,
    OrderStatus.IN_PRODUCTION,
    OrderStatus.READY,
)


async def send_weekly_order_summaries(now: datetime | None = None, sender=None) -> int:
    """Email each opted-in customer's admin contacts their open orders.

    Runs Mondays. Only customers with ``weekly_summary_enabled`` (off by
    default, toggled on the customer edit form) and at least one open
    order get a mail; recipients are resolved by
    :func:`app.services.notification_service.build_weekly_summary`, which
    honours each contact's ``weekly_summary`` consent (§19). Returns the
    number of emails sent.
    """
    from app.email.sender import build_sender

    settings = get_settings()
    current = now or datetime.now(UTC)
    iso = current.isocalendar()
    week_key = f"{iso.year}-W{iso.week:02d}"
    mail = sender if sender is not None else build_sender(settings)

    engine = _owner_engine()
    try:
        async with engine.connect() as lock_conn:
            got_lock = (
                await lock_conn.execute(
                    text("SELECT pg_try_advisory_lock(:id)"), {"id": WEEKLY_SUMMARY_LOCK_ID}
                )
            ).scalar()
            if not got_lock:
                log.info("periodic.weekly_summary.skipped", reason="lock held")
                return 0
            try:
                sent = await _run_weekly_summaries(engine, week_key, mail, settings)
            finally:
                await lock_conn.execute(
                    text("SELECT pg_advisory_unlock(:id)"), {"id": WEEKLY_SUMMARY_LOCK_ID}
                )
        log.info("periodic.weekly_summary.done", sent=sent, week=week_key)
        return sent
    finally:
        await engine.dispose()


async def _run_weekly_summaries(engine, week_key: str, mail, settings) -> int:
    from app.models.customer import Customer
    from app.models.tenant import Tenant
    from app.services.notification_service import build_weekly_summary
    from app.tasks.email_tasks import send_order_notifications
    from app.urls import tenant_base_url

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        tenant_ids = (
            (
                await session.execute(
                    select(Customer.tenant_id)
                    .join(Tenant, Tenant.id == Customer.tenant_id)
                    .where(
                        Tenant.is_active.is_(True),
                        Customer.is_active.is_(True),
                        Customer.weekly_summary_enabled.is_(True),
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
        )

    sent = 0
    for tenant_id in tenant_ids:
        async with sm() as session, session.begin():
            tenant = (
                await session.execute(select(Tenant).where(Tenant.id == tenant_id))
            ).scalar_one()
            if (tenant.settings or {}).get(WEEKLY_SUMMARY_SENT_KEY) == week_key:
                continue
            customers = (
                (
                    await session.execute(
                        select(Customer).where(
                            Customer.tenant_id == tenant_id,
                            Customer.is_active.is_(True),
                            Customer.weekly_summary_enabled.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )
            base_url = tenant_base_url(settings, tenant)
            payloads = []
            for customer in customers:
                orders = (
                    (
                        await session.execute(
                            select(Order)
                            .where(
                                Order.tenant_id == tenant_id,
                                Order.customer_id == customer.id,
                                Order.status.in_(WEEKLY_SUMMARY_STATUSES),
                            )
                            .order_by(Order.created_at)
                        )
                    )
                    .scalars()
                    .all()
                )
                payloads.extend(
                    await build_weekly_summary(
                        session,
                        tenant=tenant,
                        customer=customer,
                        orders=orders,
                        base_url=base_url,
                        settings=settings,
                    )
                )
            send_order_notifications(mail, payloads)
            sent += len(payloads)
            tenant.settings = {**(tenant.settings or {}), WEEKLY_SUMMARY_SENT_KEY: week_key}
            log.info(
                "periodic.weekly_summary.tenant",
                tenant_id=str(tenant_id),
                customers=len(customers),
                emails=len(payloads),
            )
    return sent
