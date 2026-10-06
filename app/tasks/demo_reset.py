"""Nightly reset of the public demo tenant (CEO decision E3).

Re-runs :func:`app.demo.seed.seed_demo` for ``PUBLIC_DEMO_TENANT`` every
night at 02:30 Europe/Prague: every order, comment, file and material
movement a visitor created is gone, the dates are relative to "today"
again, and every S3 object under the tenant's prefix that the new seed
did not create (visitors' uploads and their thumbnails) is deleted.

Safety: the seed refuses a tenant without its ``demo_seed`` marker
(no ``force``), so a misconfigured slug can never wipe a real customer.
The login password is random each night — nobody needs it, the public
entry is ``/demo``. Visitors' sessions die with the re-created users and
land back on the ``/demo`` chooser.

Owner role (bypasses RLS) and a ``pg_try_advisory_lock`` so only one
worker resets, like every periodic job.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.logging import get_logger

log = get_logger("app.tasks.demo_reset")

#: Distinct from every other job's lock (tests/test_advisory_lock_ids.py).
DEMO_RESET_LOCK_ID = 42_201


async def reset_public_demo(engine: Any = None) -> dict[str, Any] | None:
    """Re-seed the public demo tenant; return a summary, or None when skipped."""
    from app.demo import guard
    from app.demo.seed import DemoSeedRefused, seed_demo

    settings = get_settings()
    slug = guard.configured_slug(settings)
    if not slug:
        return None

    own_engine = engine is None
    if engine is None:
        engine = create_async_engine(settings.database_owner_url, future=True)
    try:
        async with engine.connect() as lock_conn:
            got_lock = (
                await lock_conn.execute(
                    text("SELECT pg_try_advisory_lock(:id)"), {"id": DEMO_RESET_LOCK_ID}
                )
            ).scalar()
            if not got_lock:
                log.info("periodic.demo_reset.skipped", reason="lock held")
                return None
            try:
                exists = (
                    await lock_conn.execute(
                        text("SELECT 1 FROM tenants WHERE slug = :slug"), {"slug": slug}
                    )
                ).first()
                if exists is None:
                    # Creating it is the operator's call (python -m app.demo.seed).
                    log.warning("periodic.demo_reset.missing_tenant", slug=slug)
                    return None
                try:
                    result = await seed_demo(slug=slug, engine=engine)
                except DemoSeedRefused:
                    log.error(
                        "periodic.demo_reset.refused",
                        slug=slug,
                        reason="tenant has no demo_seed marker; PUBLIC_DEMO_TENANT is wrong",
                    )
                    return None
            finally:
                await lock_conn.execute(
                    text("SELECT pg_advisory_unlock(:id)"), {"id": DEMO_RESET_LOCK_ID}
                )
    finally:
        if own_engine:
            await engine.dispose()

    guard.reset_cache()
    summary = {
        "slug": slug,
        "orders": result.orders,
        "attachments": result.attachments,
        "s3_objects_deleted": result.s3_objects_deleted,
    }
    log.info("periodic.demo_reset.done", **summary)
    return summary
