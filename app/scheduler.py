"""APScheduler setup — in-process, no Redis.

Registered jobs run inside the same process as the FastAPI app. For
horizontal scaling the advisory lock inside each job makes sure only one
worker actually does the work on any given tick.

Called from `main.lifespan`; starting and shutdown are wired up there.
"""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.logging import get_logger
from app.tasks.periodic import auto_close_delivered_orders, cleanup_stale_invited_contacts

log = get_logger("app.scheduler")


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

    log.info(
        "scheduler.configured",
        jobs=[j.id for j in scheduler.get_jobs()],
    )
    return scheduler
