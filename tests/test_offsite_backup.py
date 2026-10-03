"""Off-site backup tool (app.ops.offsite_backup) against an in-process moto S3."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws

from app.ops import offsite_backup as ob


@pytest.fixture
def s3():
    with mock_aws():
        primary = boto3.client("s3", region_name="us-east-1")
        backup = boto3.client("s3", region_name="us-east-1")
        primary.create_bucket(Bucket="primary")
        backup.create_bucket(Bucket="backups")
        yield primary, ob.BackupTarget(client=backup, bucket="backups")


def test_upload_streams_encrypted_dump_under_pg_prefix(s3):
    _, target = s3
    key = ob.upload_dump(target, "portal-1.sql.gz.gpg", io.BytesIO(b"x" * 4096))
    assert key == "pg/portal-1.sql.gz.gpg"
    assert target.client.head_object(Bucket="backups", Key=key)["ContentLength"] == 4096


def test_upload_refuses_unencrypted_dump(s3):
    _, target = s3
    with pytest.raises(ValueError, match="unencrypted"):
        ob.upload_dump(target, "portal-1.sql.gz", io.BytesIO(b"x" * 4096))


@pytest.mark.parametrize("name", ["", "../etc/passwd.gpg", "a/b.gpg"])
def test_upload_rejects_names_that_escape_the_prefix(s3, name):
    _, target = s3
    with pytest.raises(ValueError):
        ob.upload_dump(target, name, io.BytesIO(b"x" * 4096))


def test_upload_rejects_a_truncated_dump(s3):
    _, target = s3
    with pytest.raises(RuntimeError, match="only"):
        ob.upload_dump(target, "tiny.sql.gz.gpg", io.BytesIO(b"x"))


def test_sync_copies_new_and_changed_objects_and_never_deletes(s3):
    primary, target = s3
    primary.put_object(Bucket="primary", Key="t1/a.pdf", Body=b"aaaa")
    primary.put_object(Bucket="primary", Key="t1/b.png", Body=b"bb")

    assert ob.sync_attachments(primary, "primary", target) == (2, 0)
    assert ob.sync_attachments(primary, "primary", target) == (0, 2)

    # A changed object is re-copied; an object deleted from the primary
    # bucket stays in the backup — that is the point of having one.
    primary.put_object(Bucket="primary", Key="t1/b.png", Body=b"bbbbbb")
    primary.delete_object(Bucket="primary", Key="t1/a.pdf")
    assert ob.sync_attachments(primary, "primary", target) == (1, 0)

    keys = ob._list(target.client, "backups", ob.ATTACHMENTS_PREFIX)
    assert keys == {"attachments/t1/a.pdf": 4, "attachments/t1/b.png": 6}


def test_sync_handles_more_than_one_listing_page(s3):
    primary, target = s3
    for i in range(1005):
        primary.put_object(Bucket="primary", Key=f"k/{i}", Body=b"z")
    assert ob.sync_attachments(primary, "primary", target) == (1005, 0)


def test_latest_dump_age(s3):
    _, target = s3
    assert ob.latest_dump_age_hours(target) is None
    ob.upload_dump(target, "p.sql.gz.gpg", io.BytesIO(b"x" * 1024))
    age = ob.latest_dump_age_hours(target, now=datetime.now(UTC) + timedelta(hours=40))
    assert 39.9 < age < 40.1


# ---------- /healthz/backups -------------------------------------------------


async def _probe(**overrides):
    from fastapi import Response

    from app.config import Settings
    from app.ops import router as ops_router

    ops_router._cache.clear()
    response = Response()
    body = await ops_router.backups_health(response, Settings(**overrides))
    return response.status_code, body["status"]


async def test_backups_probe_is_disabled_outside_production_when_unconfigured():
    assert await _probe(APP_ENV="development") == (200, "disabled")


async def test_backups_probe_fails_in_production_when_unconfigured():
    assert await _probe(APP_ENV="production", APP_SECRET_KEY="x" * 40) == (503, "unconfigured")


async def test_backups_probe_reports_stale_and_ok(monkeypatch):
    from app.ops import router as ops_router

    for result, code in (("stale", 503), ("ok", 200), ("error", 503)):
        monkeypatch.setattr(ops_router, "_check", lambda _s, r=result: r)
        assert await _probe(BACKUP_S3_BUCKET="b", BACKUP_S3_ENDPOINT_URL="http://x") == (
            code,
            result,
        )


def test_fetch_latest_round_trips_the_dump(s3):
    _, target = s3
    ob.upload_dump(target, "a.sql.gz.gpg", io.BytesIO(b"old" * 400))
    ob.upload_dump(target, "b.sql.gz.gpg", io.BytesIO(b"new" * 400))
    key = ob.latest_dump_key(target)
    out = io.BytesIO()
    assert ob.fetch_dump(target, key, out) == 1200
    assert out.getvalue() in (b"old" * 400, b"new" * 400)  # moto timestamps may tie


def test_fetch_refuses_keys_outside_pg(s3):
    _, target = s3
    with pytest.raises(ValueError):
        ob.fetch_dump(target, "attachments/t1/a.pdf", io.BytesIO())
