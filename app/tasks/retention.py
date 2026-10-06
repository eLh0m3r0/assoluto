"""Data retention job (CEO decision D6; audit BIZ-08 / BE-19 / F-26).

Terms and Privacy promise that a deactivated tenant's data is deleted
30 days after deactivation, and the audit log is kept for three years.
Nothing enforced either. This daily job does, in three phases:

1. **Deactivated tenants** — ``is_active = false`` for more than
   :data:`TENANT_GRACE` (``tenants.deactivated_at``, stamped by a DB
   trigger from migration 1010). Every tenant-scoped business row is
   deleted (orders and everything under them, customers, contacts,
   products, assets, users, memberships, audit events, queued mail) and
   every S3 object under the tenant's ``storage_prefix`` — in the primary
   bucket and, when off-site backup is configured, every version of the
   tenant's file copies in the backup bucket (the encrypted DB dumps age
   out by the bucket's lifecycle rule). The tenant row itself, its
   subscription and its invoices are **kept**: invoices are accounting
   records with their own statutory retention. The row is marked
   ``settings._purged_at`` so later runs skip it — only once the backup
   copies are gone too, so a failed backup purge is retried next run.
2. **Audit events** older than :data:`AUDIT_RETENTION` (3 years).
3. **Orphaned S3 objects** — attachment / thumbnail objects that no
   ``order_attachments`` row references and that are older than
   :data:`ORPHAN_MIN_AGE` (so an upload in flight is never touched).
   Only keys with the attachment/thumbnail shape are considered.

**Dry-run by default.** Unless ``RETENTION_ENFORCE=true`` the job only
logs what it *would* delete (``retention.*.would_delete`` lines with
counts and tenant slugs), so the operator can review a few runs before
turning it on. Runs as the owner role (bypasses RLS) under advisory lock
42_010.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from app.config import get_settings
from app.logging import get_logger

log = get_logger("app.tasks.retention")

RETENTION_LOCK_ID = 42_010
TENANT_GRACE = timedelta(days=30)
AUDIT_RETENTION = timedelta(days=3 * 365)
ORPHAN_MIN_AGE = timedelta(days=7)

#: Tenant-scoped tables, children first (``orders`` RESTRICTs
#: ``customers``). Subscriptions and invoices are deliberately absent.
_TENANT_TABLES: tuple[str, ...] = (
    "email_outbox",
    "audit_events",
    "asset_movements",
    "assets",
    "order_attachments",
    "order_comments",
    "order_status_history",
    "order_items",
    "orders",
    "products",
    "platform_tenant_memberships",
    "customer_contacts",
    "customers",
    "users",
)

_UUID = r"[0-9a-fA-F-]{36}"
_ATTACHMENT_KEY = re.compile(
    rf"/orders/{_UUID}/(attachments/{_UUID}/[^/]+|thumbnails/{_UUID}\.jpg)$"
)


def _owner_engine():
    from sqlalchemy.ext.asyncio import create_async_engine

    return create_async_engine(get_settings().database_owner_url, future=True)


async def _purge_backup_copies(storage_prefix: str, enforce: bool) -> int:
    """Count (dry-run) or delete one tenant's file copies in the backup bucket.

    Returns 0 when off-site backup is not configured. Raises on failure so
    the caller can leave the tenant unmarked and retry on the next run.
    """
    from functools import partial

    import anyio

    from app.ops import offsite_backup

    target = offsite_backup.backup_target_from_settings()
    if target is None:
        return 0
    return await anyio.to_thread.run_sync(
        partial(offsite_backup.purge_tenant_copies, target, storage_prefix, enforce=enforce)
    )


async def _purge_tenants(engine, now: datetime, enforce: bool, stats: dict[str, Any]) -> None:
    from app.storage import s3 as s3_storage

    async with engine.connect() as conn:
        due = (
            await conn.execute(
                text(
                    "SELECT id, slug, storage_prefix, deactivated_at FROM tenants "
                    "WHERE is_active = false AND deactivated_at IS NOT NULL "
                    "AND deactivated_at < :cutoff "
                    "AND (settings->>'_purged_at') IS NULL "
                    "ORDER BY deactivated_at"
                ),
                {"cutoff": now - TENANT_GRACE},
            )
        ).all()

    for tenant_id, slug, storage_prefix, deactivated_at in due:
        counts: dict[str, int] = {}
        async with engine.begin() as conn:
            for table in _TENANT_TABLES:
                if enforce:
                    result = await conn.execute(
                        text(f"DELETE FROM {table} WHERE tenant_id = :tid"),
                        {"tid": tenant_id},
                    )
                    counts[table] = result.rowcount or 0
                else:
                    counts[table] = (
                        await conn.execute(
                            text(f"SELECT count(*) FROM {table} WHERE tenant_id = :tid"),
                            {"tid": tenant_id},
                        )
                    ).scalar_one()

        prefix = (storage_prefix or "").rstrip("/") + "/"
        objects: list[str] = []
        if prefix != "/":
            try:
                objects = [o["key"] for o in await s3_storage.list_objects_async(prefix)]
                if enforce and objects:
                    await s3_storage.delete_objects_async(objects)
            except Exception as exc:
                # The DB purge stands; leftover objects have no row any
                # more, so the orphan sweep removes them on a later run.
                log.warning(
                    "retention.tenant_s3_failed",
                    tenant=slug,
                    error_class=type(exc).__name__,
                )

        # Copies of the tenant's drawings in the off-site backup bucket are
        # part of "the data is permanently deleted" (Terms §4, DPA). Unlike
        # the primary bucket there is no orphan sweep there, so a failure
        # leaves the tenant unmarked and the whole purge is retried — the
        # DB and primary-bucket steps above are idempotent.
        backup_ok = True
        backup_versions = 0
        if prefix != "/":
            try:
                backup_versions = await _purge_backup_copies(prefix, enforce)
            except Exception as exc:
                backup_ok = False
                log.warning(
                    "retention.tenant_backup_failed",
                    tenant=slug,
                    error_class=type(exc).__name__,
                )

        if enforce and backup_ok:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE tenants SET settings = COALESCE(settings, '{}'::jsonb) "
                        "|| jsonb_build_object('_purged_at', CAST(:ts AS text)) WHERE id = :tid"
                    ),
                    {"ts": now.isoformat(), "tid": tenant_id},
                )

        log.info(
            "retention.tenant.deleted" if enforce else "retention.tenant.would_delete",
            tenant=slug,
            tenant_id=str(tenant_id),
            deactivated_at=deactivated_at.isoformat() if deactivated_at else None,
            rows=sum(counts.values()),
            s3_objects=len(objects),
            backup_object_versions=backup_versions,
            **{f"rows_{k}": v for k, v in counts.items() if v},
        )
        stats["tenants"].append(slug)
        stats["tenant_rows"] += sum(counts.values())
        stats["tenant_objects"] += len(objects)
        stats["tenant_backup_versions"] += backup_versions


async def _purge_audit(engine, now: datetime, enforce: bool, stats: dict[str, Any]) -> None:
    cutoff = now - AUDIT_RETENTION
    async with engine.begin() as conn:
        if enforce:
            result = await conn.execute(
                text("DELETE FROM audit_events WHERE occurred_at < :cutoff"), {"cutoff": cutoff}
            )
            stats["audit_events"] = result.rowcount or 0
        else:
            stats["audit_events"] = (
                await conn.execute(
                    text("SELECT count(*) FROM audit_events WHERE occurred_at < :cutoff"),
                    {"cutoff": cutoff},
                )
            ).scalar_one()
    if stats["audit_events"]:
        log.info(
            "retention.audit.deleted" if enforce else "retention.audit.would_delete",
            count=stats["audit_events"],
            older_than=cutoff.isoformat(),
        )


async def _sweep_orphans(engine, now: datetime, enforce: bool, stats: dict[str, Any]) -> None:
    from app.storage import s3 as s3_storage

    try:
        objects = await s3_storage.list_objects_async("")
    except Exception as exc:
        log.warning("retention.orphans.list_failed", error_class=type(exc).__name__)
        return

    async with engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT storage_key, thumbnail_key FROM order_attachments"))
        ).all()
    referenced: set[str] = set()
    for storage_key, thumbnail_key in rows:
        referenced.add(storage_key)
        if thumbnail_key:
            referenced.add(thumbnail_key)

    cutoff = now - ORPHAN_MIN_AGE
    orphans = [
        o["key"]
        for o in objects
        if _ATTACHMENT_KEY.search(o["key"])
        and o["key"] not in referenced
        and o["last_modified"] < cutoff
    ]
    stats["orphan_objects"] = len(orphans)
    if not orphans:
        return
    if enforce:
        await s3_storage.delete_objects_async(orphans)
    log.info(
        "retention.orphans.deleted" if enforce else "retention.orphans.would_delete",
        count=len(orphans),
        sample=orphans[:5],
    )


async def enforce_retention(
    now: datetime | None = None, enforce: bool | None = None
) -> dict[str, Any]:
    """Run all three phases. Returns per-phase counts (would-be counts in
    dry-run). ``enforce`` defaults to ``RETENTION_ENFORCE``."""
    settings = get_settings()
    current = now or datetime.now(UTC)
    do_delete = settings.retention_enforce if enforce is None else enforce
    stats: dict[str, Any] = {
        "enforce": do_delete,
        "tenants": [],
        "tenant_rows": 0,
        "tenant_objects": 0,
        "tenant_backup_versions": 0,
        "audit_events": 0,
        "orphan_objects": 0,
    }

    engine = _owner_engine()
    try:
        async with engine.connect() as lock_conn:
            got_lock = (
                await lock_conn.execute(
                    text("SELECT pg_try_advisory_lock(:id)"), {"id": RETENTION_LOCK_ID}
                )
            ).scalar()
            await lock_conn.commit()
            if not got_lock:
                log.info("retention.skipped", reason="lock held")
                return stats
            try:
                await _purge_tenants(engine, current, do_delete, stats)
                await _purge_audit(engine, current, do_delete, stats)
                await _sweep_orphans(engine, current, do_delete, stats)
            finally:
                await lock_conn.execute(
                    text("SELECT pg_advisory_unlock(:id)"), {"id": RETENTION_LOCK_ID}
                )
                await lock_conn.commit()
    finally:
        await engine.dispose()

    log.info("retention.done", **{k: v for k, v in stats.items() if k != "tenants"})
    return stats
