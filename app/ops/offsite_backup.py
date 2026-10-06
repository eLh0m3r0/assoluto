"""Off-site backup: encrypted DB dumps + attachment copies in a second location.

Runs inside the web container, which already has boto3 and the S3
credentials — the VPS host has neither rclone nor root access for the
deploy user. ``scripts/backup.sh`` (nightly cron) pipes the GPG-encrypted
dump into ``upload`` and then calls ``sync-attachments``::

    docker compose exec -T web python -m app.ops.offsite_backup upload --name X.sql.gz.gpg < X
    docker compose exec -T web python -m app.ops.offsite_backup sync-attachments
    docker compose exec -T web python -m app.ops.offsite_backup status
    docker compose exec -T web python -m app.ops.offsite_backup fetch --latest > dump.sql.gz.gpg

The backup bucket lives in a different Hetzner location from the primary
bucket and the VPS (``BACKUP_S3_ENDPOINT_URL``), has versioning enabled,
and is append-only from the backup path: ``upload`` and ``sync-attachments``
never delete, so a wiped primary bucket or a wiped ``/backups`` directory
cannot propagate. Retention of dumps is the bucket's lifecycle rule.

The single exception is :func:`purge_tenant_copies`, called only by the
retention job (``app.tasks.retention``) when a tenant deactivated more than
30 days ago is purged: the Terms and the DPA promise that its data is then
deleted, and copies of its drawings in this bucket are part of that data.
It removes every *version* under exactly ``attachments/<storage_prefix>``,
so the versioned bucket keeps no hidden copy either.

Before this existed the nightly dumps sat on the same disk as the database
and the customers' drawings had no backup at all (audit 2026-10-03 BE-01).
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, BinaryIO

DB_PREFIX = "pg/"
ATTACHMENTS_PREFIX = "attachments/"


@dataclass(frozen=True)
class BackupTarget:
    client: Any
    bucket: str


def backup_target_from_settings() -> BackupTarget | None:
    """The backup target, or ``None`` when off-site backup is not configured."""
    import boto3
    from botocore.config import Config

    from app.config import get_settings

    settings = get_settings()
    if not settings.backup_s3_bucket or not settings.backup_s3_endpoint_url:
        return None
    backup_client = boto3.client(
        "s3",
        endpoint_url=settings.backup_s3_endpoint_url,
        aws_access_key_id=settings.backup_s3_access_key or settings.s3_access_key,
        aws_secret_access_key=settings.backup_s3_secret_key or settings.s3_secret_key,
        # Hetzner rejects requests whose region differs from the location.
        region_name=settings.backup_s3_region,
        config=Config(s3={"addressing_style": "virtual"}, retries={"max_attempts": 5}),
    )
    return BackupTarget(client=backup_client, bucket=settings.backup_s3_bucket)


def _clients_from_settings() -> tuple[Any, str, BackupTarget]:
    """Primary S3 client + bucket, and the backup target, from settings."""
    from app.config import get_settings
    from app.storage.s3 import get_s3_client

    target = backup_target_from_settings()
    if target is None:
        raise SystemExit("BACKUP_S3_BUCKET / BACKUP_S3_ENDPOINT_URL are not set")
    return get_s3_client(), get_settings().s3_bucket, target


def upload_dump(
    target: BackupTarget, name: str, stream: BinaryIO, *, allow_plain: bool = False
) -> str:
    """Stream a dump into ``pg/<name>``. Refuses unencrypted dumps by default."""
    if "/" in name or not name:
        raise ValueError(f"invalid dump name: {name!r}")
    if not name.endswith(".gpg") and not allow_plain:
        raise ValueError("refusing to ship an unencrypted dump off-site (set BACKUP_GPG_RECIPIENT)")
    key = DB_PREFIX + name
    target.client.upload_fileobj(stream, target.bucket, key)
    head = target.client.head_object(Bucket=target.bucket, Key=key)
    if head["ContentLength"] < 512:
        raise RuntimeError(f"uploaded dump {key} is only {head['ContentLength']} bytes")
    return key


def _list(client: Any, bucket: str, prefix: str = "") -> dict[str, int]:
    sizes: dict[str, int] = {}
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            sizes[obj["Key"]] = obj["Size"]
    return sizes


def sync_attachments(
    source_client: Any, source_bucket: str, target: BackupTarget
) -> tuple[int, int]:
    """Copy primary-bucket objects that are missing (or differ in size) in the backup.

    Never deletes: an object removed from the primary bucket stays in the
    backup. Returns ``(copied, already_present)``.
    """
    existing = _list(target.client, target.bucket, ATTACHMENTS_PREFIX)
    copied = present = 0
    for key, size in _list(source_client, source_bucket).items():
        dest = ATTACHMENTS_PREFIX + key
        if existing.get(dest) == size:
            present += 1
            continue
        body = source_client.get_object(Bucket=source_bucket, Key=key)["Body"]
        target.client.upload_fileobj(body, target.bucket, dest)
        copied += 1
    return copied, present


def purge_tenant_copies(target: BackupTarget, storage_prefix: str, *, enforce: bool) -> int:
    """Delete every version of every backup copy under one tenant's prefix.

    Only ``attachments/<storage_prefix>/`` is touched — never ``pg/`` (the
    encrypted dumps age out by lifecycle rule) and never another tenant.
    In dry-run (``enforce=False``) nothing is deleted. Returns the number
    of object versions and delete markers found.
    """
    prefix = (storage_prefix or "").strip("/")
    if not prefix:
        raise ValueError("refusing to purge the whole attachments/ tree")
    full_prefix = f"{ATTACHMENTS_PREFIX}{prefix}/"
    found: list[dict[str, str]] = []
    paginator = target.client.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=target.bucket, Prefix=full_prefix):
        for entry in page.get("Versions", []) + page.get("DeleteMarkers", []):
            found.append({"Key": entry["Key"], "VersionId": entry["VersionId"]})
    if enforce:
        for start in range(0, len(found), 1000):
            resp = target.client.delete_objects(
                Bucket=target.bucket,
                Delete={"Objects": found[start : start + 1000], "Quiet": True},
            )
            if resp.get("Errors"):
                raise RuntimeError(f"backup purge failed for {len(resp['Errors'])} object(s)")
    return len(found)


def latest_dump_age_hours(target: BackupTarget, *, now: datetime | None = None) -> float | None:
    """Age of the newest ``pg/`` object in hours, or ``None`` if there is none."""
    newest: datetime | None = None
    paginator = target.client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=target.bucket, Prefix=DB_PREFIX):
        for obj in page.get("Contents", []):
            if newest is None or obj["LastModified"] > newest:
                newest = obj["LastModified"]
    if newest is None:
        return None
    return ((now or datetime.now(UTC)) - newest).total_seconds() / 3600


def latest_dump_key(target: BackupTarget) -> str | None:
    newest: tuple[datetime, str] | None = None
    paginator = target.client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=target.bucket, Prefix=DB_PREFIX):
        for obj in page.get("Contents", []):
            if newest is None or obj["LastModified"] > newest[0]:
                newest = (obj["LastModified"], obj["Key"])
    return newest[1] if newest else None


def fetch_dump(target: BackupTarget, key: str, out: BinaryIO) -> int:
    """Stream ``key`` (``pg/...``) to ``out``; returns bytes written."""
    if not key.startswith(DB_PREFIX):
        raise ValueError("only dumps under pg/ can be fetched")
    body = target.client.get_object(Bucket=target.bucket, Key=key)["Body"]
    written = 0
    for chunk in iter(lambda: body.read(1 << 20), b""):
        out.write(chunk)
        written += len(chunk)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.ops.offsite_backup")
    sub = parser.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("upload", help="upload an encrypted dump read from stdin")
    up.add_argument("--name", required=True)
    up.add_argument("--allow-plain", action="store_true")
    sub.add_parser("sync-attachments", help="copy new attachments to the backup bucket")
    fe = sub.add_parser("fetch", help="write a dump to stdout (for restores)")
    grp = fe.add_mutually_exclusive_group(required=True)
    grp.add_argument("--key")
    grp.add_argument("--latest", action="store_true")
    st = sub.add_parser("status", help="exit 1 if the newest dump is too old")
    st.add_argument("--max-age-hours", type=float, default=30.0)
    args = parser.parse_args(argv)

    source_client, source_bucket, target = _clients_from_settings()
    started = time.monotonic()
    if args.cmd == "upload":
        key = upload_dump(target, args.name, sys.stdin.buffer, allow_plain=args.allow_plain)
        print(f"[offsite] uploaded {target.bucket}/{key} in {time.monotonic() - started:.1f}s")
        return 0
    if args.cmd == "sync-attachments":
        copied, present = sync_attachments(source_client, source_bucket, target)
        print(f"[offsite] attachments: {copied} copied, {present} already present")
        return 0
    if args.cmd == "fetch":
        fetch_key: str | None = latest_dump_key(target) if args.latest else args.key
        if fetch_key is None:
            print("[offsite] no dump found", file=sys.stderr)
            return 1
        written = fetch_dump(target, fetch_key, sys.stdout.buffer)
        print(f"[offsite] fetched {fetch_key} ({written} bytes)", file=sys.stderr)
        return 0
    age = latest_dump_age_hours(target)
    if age is None:
        print("[offsite] no dump found")
        return 1
    print(f"[offsite] newest dump is {age:.1f} h old")
    return 0 if age <= args.max_age_hours else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
