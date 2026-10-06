"""Retention job (D6; audit BIZ-08 / BE-19)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.storage import s3 as s3_mod
from app.tasks.retention import enforce_retention
from tests.test_attachments_flow import _point_settings_at_moto as _moto_env
from tests.test_attachments_flow import mock_s3 as _mock_s3

pytestmark = pytest.mark.postgres

mock_s3 = _mock_s3
_point_settings_at_moto = _moto_env

NOW = datetime.now(UTC)


async def _tenant(conn, slug: str, *, active: bool, deactivated_days_ago: int | None) -> dict:
    tid = uuid4()
    deactivated_at = (
        NOW - timedelta(days=deactivated_days_ago) if deactivated_days_ago is not None else None
    )
    await conn.execute(
        text(
            "INSERT INTO tenants (id, slug, name, billing_email, storage_prefix, is_active, "
            "deactivated_at) VALUES (:id, :slug, :slug, 'b@x.example', :prefix, :active, :d)"
        ),
        {
            "id": tid,
            "slug": slug,
            "prefix": f"tenants/{slug}/",
            "active": active,
            "d": deactivated_at,
        },
    )
    cid, oid, aid, uid = uuid4(), uuid4(), uuid4(), uuid4()
    await conn.execute(
        text(
            "INSERT INTO users (id, tenant_id, email, full_name, role) "
            "VALUES (:id, :t, :e, 'U', 'tenant_admin')"
        ),
        {"id": uid, "t": tid, "e": f"u@{slug}.example"},
    )
    await conn.execute(
        text("INSERT INTO customers (id, tenant_id, name) VALUES (:id, :t, 'C')"),
        {"id": cid, "t": tid},
    )
    await conn.execute(
        text(
            "INSERT INTO orders (id, tenant_id, customer_id, number, title, status) "
            "VALUES (:id, :t, :c, :n, 'O', 'draft')"
        ),
        {"id": oid, "t": tid, "c": cid, "n": f"{slug}-1"},
    )
    key = f"tenants/{slug}/orders/{oid}/attachments/{aid}/d.pdf"
    await conn.execute(
        text(
            "INSERT INTO order_attachments (id, tenant_id, order_id, kind, filename, "
            "content_type, size_bytes, storage_key) VALUES "
            "(:id, :t, :o, 'drawing', 'd.pdf', 'application/pdf', 3, :k)"
        ),
        {"id": aid, "t": tid, "o": oid, "k": key},
    )
    await conn.execute(
        text(
            "INSERT INTO audit_events (id, tenant_id, occurred_at, actor_type, actor_label, "
            "action, entity_type, entity_id, entity_label) VALUES "
            "(:id, :t, :at, 'system', 'system', 'x.y', 'order', :o, 'o')"
        ),
        {"id": uuid4(), "t": tid, "at": NOW, "o": oid},
    )
    s3_mod.upload_bytes(key, b"pdf")
    return {"id": tid, "key": key}


async def _count(owner_engine, sql: str, **params) -> int:
    async with owner_engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


def _keys() -> set[str]:
    return {o["key"] for o in s3_mod.list_objects("")}


@pytest.fixture
async def world(owner_engine, wipe_db, mock_s3):
    async with owner_engine.begin() as conn:
        active = await _tenant(conn, "alive", active=True, deactivated_days_ago=None)
        gone = await _tenant(conn, "gone", active=False, deactivated_days_ago=40)
        recent = await _tenant(conn, "recent", active=False, deactivated_days_ago=10)
        plan_id = (
            await conn.execute(text("SELECT id FROM platform_plans WHERE code = 'starter'"))
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO platform_subscriptions (id, tenant_id, plan_id, status) "
                "VALUES (:id, :t, :p, 'canceled')"
            ),
            {"id": uuid4(), "t": gone["id"], "p": plan_id},
        )
        # A 4-year-old audit event on the live tenant.
        await conn.execute(
            text(
                "INSERT INTO audit_events (id, tenant_id, occurred_at, actor_type, actor_label, "
                "action, entity_type, entity_id, entity_label) VALUES "
                "(:id, :t, :at, 'system', 'system', 'old.event', 'tenant', :t, 'x')"
            ),
            {"id": uuid4(), "t": active["id"], "at": NOW - timedelta(days=4 * 365)},
        )
    orphan = f"tenants/alive/orders/{uuid4()}/attachments/{uuid4()}/lost.pdf"
    s3_mod.upload_bytes(orphan, b"lost")
    s3_mod.upload_bytes("backups/not-an-attachment.sql", b"x")
    return {"active": active, "gone": gone, "recent": recent, "orphan": orphan}


async def test_dry_run_reports_and_deletes_nothing(owner_engine, world) -> None:
    before = _keys()
    stats = await enforce_retention(now=NOW + timedelta(days=8))

    assert stats["enforce"] is False  # RETENTION_ENFORCE defaults off
    assert stats["tenants"] == ["gone"]
    assert stats["tenant_rows"] > 0
    assert stats["audit_events"] == 1
    assert stats["orphan_objects"] == 1
    assert _keys() == before
    assert await _count(owner_engine, "SELECT count(*) FROM orders") == 3
    assert await _count(owner_engine, "SELECT count(*) FROM audit_events") == 4


async def test_enforce_purges_only_what_is_due(owner_engine, world) -> None:
    stats = await enforce_retention(now=NOW + timedelta(days=8), enforce=True)
    gone_id = world["gone"]["id"]

    assert stats["tenants"] == ["gone"]
    for table in ("orders", "customers", "users", "order_attachments", "audit_events"):
        assert (
            await _count(
                owner_engine, f"SELECT count(*) FROM {table} WHERE tenant_id = :t", t=gone_id
            )
            == 0
        ), table
    # Tenant row and billing records are kept; the row is marked purged.
    assert await _count(owner_engine, "SELECT count(*) FROM tenants WHERE id = :t", t=gone_id) == 1
    assert (
        await _count(
            owner_engine,
            "SELECT count(*) FROM platform_subscriptions WHERE tenant_id = :t",
            t=gone_id,
        )
        == 1
    )
    assert (
        await _count(
            owner_engine,
            "SELECT count(*) FROM tenants WHERE id = :t AND settings ? '_purged_at'",
            t=gone_id,
        )
        == 1
    )

    keys = _keys()
    assert world["gone"]["key"] not in keys
    assert world["active"]["key"] in keys  # referenced
    assert world["recent"]["key"] in keys  # deactivated only 18 days "ago"
    assert world["orphan"] not in keys
    assert "backups/not-an-attachment.sql" in keys  # never swept

    # The live and the recently deactivated tenant keep their data,
    # minus the live tenant's 4-year-old audit event.
    assert await _count(owner_engine, "SELECT count(*) FROM orders") == 2
    assert (
        await _count(owner_engine, "SELECT count(*) FROM audit_events WHERE action = 'old.event'")
        == 0
    )
    assert await _count(owner_engine, "SELECT count(*) FROM audit_events") == 2

    # Idempotent: a second run finds nothing more to purge.
    again = await enforce_retention(now=NOW + timedelta(days=8), enforce=True)
    assert again["tenants"] == []
    assert again["orphan_objects"] == 0


async def test_fresh_orphans_are_left_alone(owner_engine, world) -> None:
    """An object without a row may be an upload in flight — 7-day floor."""
    stats = await enforce_retention(now=NOW, enforce=True)
    assert stats["orphan_objects"] == 0
    assert world["orphan"] in _keys()


@pytest.fixture
def backup_bucket(mock_s3, monkeypatch):
    """A versioned moto bucket standing in for the off-site backup target,
    pre-filled the way ``sync-attachments`` would leave it."""
    import boto3

    from app.ops import offsite_backup

    client = boto3.client("s3", region_name="eu-central-1")
    client.create_bucket(
        Bucket="backups", CreateBucketConfiguration={"LocationConstraint": "eu-central-1"}
    )
    client.put_bucket_versioning(Bucket="backups", VersioningConfiguration={"Status": "Enabled"})
    target = offsite_backup.BackupTarget(client=client, bucket="backups")
    monkeypatch.setattr(offsite_backup, "backup_target_from_settings", lambda: target)
    return target


def _backup_keys(target) -> set[str]:
    resp = target.client.list_object_versions(Bucket=target.bucket)
    return {e["Key"] for e in resp.get("Versions", []) + resp.get("DeleteMarkers", [])}


async def test_purge_also_deletes_backup_copies_of_the_purged_tenant(
    owner_engine, world, backup_bucket
) -> None:
    """Terms §4 / DPA: 30 days after deactivation the data is permanently
    deleted — including the drawings' copies in the off-site bucket."""
    for tenant in ("gone", "recent", "active"):
        backup_bucket.client.put_object(
            Bucket="backups", Key=f"attachments/{world[tenant]['key']}", Body=b"pdf"
        )
    backup_bucket.client.put_object(Bucket="backups", Key="pg/portal-1.sql.gz.gpg", Body=b"d")

    dry = await enforce_retention(now=NOW + timedelta(days=8))
    assert dry["tenant_backup_versions"] == 1
    assert f"attachments/{world['gone']['key']}" in _backup_keys(backup_bucket)

    stats = await enforce_retention(now=NOW + timedelta(days=8), enforce=True)
    assert stats["tenant_backup_versions"] == 1
    assert _backup_keys(backup_bucket) == {
        f"attachments/{world['recent']['key']}",
        f"attachments/{world['active']['key']}",
        "pg/portal-1.sql.gz.gpg",
    }


async def test_failed_backup_purge_leaves_tenant_unmarked_for_retry(
    owner_engine, world, backup_bucket, monkeypatch
) -> None:
    from app.ops import offsite_backup

    def _boom(*_a, **_k):
        raise RuntimeError("backup bucket unreachable")

    monkeypatch.setattr(offsite_backup, "purge_tenant_copies", _boom)
    stats = await enforce_retention(now=NOW + timedelta(days=8), enforce=True)
    assert stats["tenants"] == ["gone"]
    gone_id = world["gone"]["id"]
    # DB rows are gone, but without the purge mark the next run retries.
    assert (
        await _count(owner_engine, "SELECT count(*) FROM orders WHERE tenant_id = :t", t=gone_id)
        == 0
    )
    assert (
        await _count(
            owner_engine,
            "SELECT count(*) FROM tenants WHERE id = :t AND settings ? '_purged_at'",
            t=gone_id,
        )
        == 0
    )

    monkeypatch.undo()
    monkeypatch.setattr(offsite_backup, "backup_target_from_settings", lambda: backup_bucket)
    again = await enforce_retention(now=NOW + timedelta(days=8), enforce=True)
    assert again["tenants"] == ["gone"]
    assert (
        await _count(
            owner_engine,
            "SELECT count(*) FROM tenants WHERE id = :t AND settings ? '_purged_at'",
            t=gone_id,
        )
        == 1
    )


async def test_deactivation_trigger_stamps_and_clears(owner_engine, wipe_db) -> None:
    tid = uuid4()
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, billing_email, storage_prefix) "
                "VALUES (:id, 'trig', 'T', 'b@x.example', 'tenants/trig/')"
            ),
            {"id": tid},
        )
        await conn.execute(text("UPDATE tenants SET is_active = false WHERE id = :id"), {"id": tid})
    assert (
        await _count(
            owner_engine,
            "SELECT count(*) FROM tenants WHERE id = :t AND deactivated_at IS NOT NULL",
            t=tid,
        )
        == 1
    )
    async with owner_engine.begin() as conn:
        await conn.execute(text("UPDATE tenants SET is_active = true WHERE id = :id"), {"id": tid})
    assert (
        await _count(
            owner_engine,
            "SELECT count(*) FROM tenants WHERE id = :t AND deactivated_at IS NULL",
            t=tid,
        )
        == 1
    )
