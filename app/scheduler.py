"""APScheduler setup — in-process, no Redis.

Registered jobs run inside the same process as the FastAPI app. For
horizontal scaling the advisory lock inside each job makes sure only one
worker actually does the work on any given tick.

Called from `main.lifespan`; starting and shutdown are wired up there.
"""

from __future__ import annotations

from apscheduler.events import EVENT_JOB_ERROR, JobExecutionEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.email.outbox import deliver_email_outbox
from app.logging import get_logger
from app.tasks.periodic import (
    auto_close_delivered_orders,
    cleanup_old_stripe_events,
    cleanup_stale_invited_contacts,
    enforce_canceled_subscriptions,
    expire_demo_trials,
    send_trial_nurture_emails,
    send_weekly_order_summaries,
)
from app.tasks.retention import enforce_retention

log = get_logger("app.scheduler")


def _on_job_error(event: JobExecutionEvent) -> None:
    """Mail the operator when a periodic job crashes (OPS_ALERT_EMAIL).

    APScheduler only logs job exceptions; a retention or outbox job that
    dies every night would otherwise go unnoticed.
    """
    if event.exception is None:
        return
    from app.config import get_settings
    from app.email.ops_alert import notify_unhandled
    from app.email.sender import build_sender

    settings = get_settings()
    if not settings.ops_alert_email:
        return
    notify_unhandled(
        event.exception,
        settings=settings,
        sender=build_sender(settings),
        where=f"scheduler:{event.job_id}",
    )


def build_scheduler() -> AsyncIOScheduler:
    """Create an `AsyncIOScheduler` with all periodic jobs registered."""
    scheduler = AsyncIOScheduler(timezone="UTC")

    scheduler.add_job(
        auto_close_delivered_orders,
        trigger=CronTrigger(minute=0),  # top of every hour
        id="auto_close_delivered_orders",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        cleanup_stale_invited_contacts,
        trigger=CronTrigger(hour=3, minute=0),  # 03:00 UTC daily
        id="cleanup_stale_invited_contacts",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=600,
    )
    scheduler.add_job(
        cleanup_old_stripe_events,
        trigger=CronTrigger(hour=3, minute=30),  # 03:30 UTC daily
        id="cleanup_old_stripe_events",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=600,
    )

    scheduler.add_job(
        expire_demo_trials,
        trigger=CronTrigger(hour=3, minute=45),  # 03:45 UTC daily
        id="expire_demo_trials",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=600,
    )

    # Daily after expire_demo_trials so the canceled rows it creates
    # are visible to this job's grace check on the same run if their
    # period_end was already > 3 days ago.
    scheduler.add_job(
        enforce_canceled_subscriptions,
        trigger=CronTrigger(hour=4, minute=0),  # 04:00 UTC daily
        id="enforce_canceled_subscriptions",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=600,
    )

    # Mid-morning CET so trial admins read it during their workday.
    # No-ops unless FEATURE_PLATFORM + TRIAL_NURTURE_ENABLED are set.
    scheduler.add_job(
        send_trial_nurture_emails,
        trigger=CronTrigger(hour=8, minute=0),  # 08:00 UTC daily
        id="send_trial_nurture_emails",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=600,
    )

    # Durable e-mail outbox (BE-09): retry anything the inline send could
    # not deliver. Every minute; rows are claimed with SKIP LOCKED.
    scheduler.add_job(
        deliver_email_outbox,
        trigger=CronTrigger(minute="*"),
        id="deliver_email_outbox",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )

    # Data retention (D6): purge tenants deactivated > 30 days, audit
    # events > 3 years, orphaned S3 objects > 7 days. Dry-run unless
    # RETENTION_ENFORCE=true. After enforce_canceled_subscriptions.
    scheduler.add_job(
        enforce_retention,
        trigger=CronTrigger(hour=4, minute=30),  # 04:30 UTC daily
        id="enforce_retention",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=3600,
    )

    # Monday 05:00 UTC (06:00/07:00 Prague) — in the inbox before the
    # customer's buyer starts the week. Only customers the supplier opted
    # in receive it (IDEA-10).
    scheduler.add_job(
        send_weekly_order_summaries,
        trigger=CronTrigger(day_of_week="mon", hour=5, minute=0),
        id="send_weekly_order_summaries",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=3600,
    )

    scheduler.add_listener(_on_job_error, EVENT_JOB_ERROR)

    # Quote follow-ups (IDEA-2). Mid-morning CET, after the trial nurture
    # run. No-ops when QUOTE_REMINDER_DAYS=0.
    from app.tasks.quote_reminders import send_quote_reminders

    scheduler.add_job(
        send_quote_reminders,
        trigger=CronTrigger(hour=8, minute=15),  # 08:15 UTC daily
        id="send_quote_reminders",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=600,
    )

    # Public demo (E3): re-seed PUBLIC_DEMO_TENANT nightly, at a quiet
    # hour for Czech visitors. No-op when the setting is empty.
    from app.tasks.demo_reset import reset_public_demo

    scheduler.add_job(
        reset_public_demo,
        trigger=CronTrigger(hour=2, minute=30, timezone="Europe/Prague"),
        id="reset_public_demo",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=3600,
    )

    log.info(
        "scheduler.configured",
        jobs=[j.id for j in scheduler.get_jobs()],
    )
    return scheduler
