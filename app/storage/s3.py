"""S3 / MinIO client helper.

Keeps boto3 configuration in one place so the rest of the app only
touches a thin wrapper. Used by the attachment upload route (put_object)
and the background thumbnail task (get_object + put_object).

### Never call boto3 on the event loop

boto3 is synchronous. The app runs a single uvicorn worker, so one
blocking ``put_object`` of a 50 MB drawing freezes every tenant's
requests, ``/healthz`` and the Stripe webhook for as long as the PUT
takes (audit BE-03 / SEC-3). Code running inside ``async def`` must use
the ``*_async`` variants below, which hop to a worker thread via
:func:`anyio.to_thread.run_sync`. The plain sync functions remain for
code that already runs in a thread (CLI scripts, threadpool helpers).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from functools import lru_cache, partial
from io import BytesIO
from typing import IO, Any

import anyio
import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.config import Settings, get_settings


@lru_cache(maxsize=1)
def get_s3_client():
    """Return a cached boto3 S3 client for internal operations.

    An empty `s3_endpoint_url` is interpreted as "use AWS defaults",
    which is how tests based on `moto` (which patches the default
    endpoints) opt into the in-process backend.
    """
    settings: Settings = get_settings()
    endpoint_url = settings.s3_endpoint_url or None
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        use_ssl=settings.s3_use_ssl,
        config=Config(signature_version="s3v4"),
    )


@lru_cache(maxsize=1)
def get_public_s3_client():
    """Return a boto3 S3 client bound to the PUBLIC endpoint.

    Used only for `generate_presigned_url`, so the URL handed to the
    browser contains a hostname the browser can actually reach. When no
    `s3_public_endpoint_url` is configured, this mirrors the internal
    client — useful for production S3 where internal and public endpoints
    are identical (e.g. https://s3.eu-central-003.backblazeb2.com).
    """
    settings: Settings = get_settings()
    public_endpoint = settings.s3_public_endpoint_url or settings.s3_endpoint_url or None
    return boto3.client(
        "s3",
        endpoint_url=public_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        use_ssl=settings.s3_use_ssl,
        config=Config(signature_version="s3v4"),
    )


def ensure_bucket_exists(bucket: str | None = None) -> None:
    """Create the application bucket if it doesn't already exist.

    Called at app startup so MinIO-on-localhost doesn't require manual
    setup. Swallows `NoSuchBucket`; re-raises anything else.
    """
    settings = get_settings()
    bucket = bucket or settings.s3_bucket
    client = get_s3_client()
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchBucket", "NotFound"):
            client.create_bucket(Bucket=bucket)
        else:
            raise


def warn_if_public_endpoint_unreachable(log) -> None:
    """Log a warning when the public S3 endpoint won't reach the
    configured bucket.

    Catches the dev-environment foot-gun where the operator copies a
    prod-style ``.env`` (custom ``S3_BUCKET`` but empty
    ``S3_PUBLIC_ENDPOINT_URL``): presigned URLs come out pointing at
    the *internal* endpoint host (``http://minio:9000``) which the
    browser can't reach, OR at a public host whose bucket name doesn't
    exist. A failed upload silently shows a broken link, the operator
    can't tell whether the file is in S3 or not, and there's no
    breadcrumb in the log. This call surfaces the misconfiguration at
    boot instead.

    Best-effort: the check itself must not block startup.
    """
    settings = get_settings()
    public_endpoint = settings.s3_public_endpoint_url or settings.s3_endpoint_url or ""
    if not public_endpoint:
        return  # AWS-default endpoints — the SDK will pick the right host.
    try:
        client = get_public_s3_client()
        client.head_bucket(Bucket=settings.s3_bucket)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        log.warning(
            "s3.public_endpoint_check_failed",
            endpoint=public_endpoint,
            bucket=settings.s3_bucket,
            error_code=code,
            hint=(
                "Presigned upload URLs use this endpoint. If S3_BUCKET is "
                "wrong or the host is unreachable from the browser, every "
                "attachment upload looks like a silent failure."
            ),
        )
    except Exception as exc:
        log.warning(
            "s3.public_endpoint_check_failed",
            endpoint=public_endpoint,
            bucket=settings.s3_bucket,
            error_class=type(exc).__name__,
        )


def upload_bytes(key: str, data: bytes, *, content_type: str = "application/octet-stream") -> None:
    """Upload raw bytes under `key`."""
    settings = get_settings()
    get_s3_client().put_object(
        Bucket=settings.s3_bucket,
        Key=key,
        Body=BytesIO(data),
        ContentType=content_type,
    )


def download_bytes(key: str) -> bytes:
    """Download an object by key and return it as bytes."""
    settings = get_settings()
    response = get_s3_client().get_object(Bucket=settings.s3_bucket, Key=key)
    return response["Body"].read()


def delete_object(key: str) -> None:
    settings = get_settings()
    get_s3_client().delete_object(Bucket=settings.s3_bucket, Key=key)


def upload_fileobj(
    key: str, fileobj: IO[bytes], *, content_type: str = "application/octet-stream"
) -> None:
    """Upload a file-like object under ``key`` without reading it into RAM.

    ``upload_fileobj`` streams in parts (multipart above 8 MB), so the
    peak memory cost of a 50 MB upload is a few MB of buffers, not the
    whole file.
    """
    settings = get_settings()
    fileobj.seek(0)
    get_s3_client().upload_fileobj(
        fileobj,
        settings.s3_bucket,
        key,
        ExtraArgs={"ContentType": content_type},
    )


def iter_object_chunks(key: str, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    """Yield an object's body in ``chunk_size`` pieces (blocking)."""
    settings = get_settings()
    body = get_s3_client().get_object(Bucket=settings.s3_bucket, Key=key)["Body"]
    try:
        while True:
            chunk = body.read(chunk_size)
            if not chunk:
                return
            yield chunk
    finally:
        body.close()


def copy_object_to(key: str, out: IO[bytes], chunk_size: int = 1024 * 1024) -> int:
    """Stream an object into the writable ``out``; return bytes written."""
    written = 0
    for chunk in iter_object_chunks(key, chunk_size):
        out.write(chunk)
        written += len(chunk)
    return written


def list_objects(prefix: str = "") -> Iterator[dict[str, Any]]:
    """Yield ``{"key", "size", "last_modified"}`` for every object under ``prefix``."""
    settings = get_settings()
    paginator = get_s3_client().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=settings.s3_bucket, Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            last_modified: datetime = obj["LastModified"]
            yield {"key": obj["Key"], "size": obj.get("Size", 0), "last_modified": last_modified}


def delete_objects(keys: list[str]) -> int:
    """Delete ``keys`` in batches of 1000 (the S3 API maximum)."""
    settings = get_settings()
    client = get_s3_client()
    deleted = 0
    for start in range(0, len(keys), 1000):
        batch = keys[start : start + 1000]
        if not batch:
            continue
        client.delete_objects(
            Bucket=settings.s3_bucket,
            Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
        )
        deleted += len(batch)
    return deleted


def head_bucket() -> None:
    """Raise if the configured bucket is unreachable."""
    settings = get_settings()
    get_s3_client().head_bucket(Bucket=settings.s3_bucket)


# ---------------------------------------------------------------- async
#
# Thin threadpool wrappers. Every ``async def`` caller uses these so a
# slow S3 endpoint stalls only the request that is waiting on it.


async def upload_bytes_async(
    key: str, data: bytes, *, content_type: str = "application/octet-stream"
) -> None:
    await anyio.to_thread.run_sync(partial(upload_bytes, key, data, content_type=content_type))


async def upload_fileobj_async(
    key: str, fileobj: IO[bytes], *, content_type: str = "application/octet-stream"
) -> None:
    await anyio.to_thread.run_sync(partial(upload_fileobj, key, fileobj, content_type=content_type))


async def download_bytes_async(key: str) -> bytes:
    return await anyio.to_thread.run_sync(download_bytes, key)


async def delete_object_async(key: str) -> None:
    await anyio.to_thread.run_sync(delete_object, key)


async def delete_objects_async(keys: list[str]) -> int:
    return await anyio.to_thread.run_sync(delete_objects, keys)


async def list_objects_async(prefix: str = "") -> list[dict[str, Any]]:
    return await anyio.to_thread.run_sync(lambda: list(list_objects(prefix)))


async def copy_object_to_async(key: str, out: IO[bytes]) -> int:
    return await anyio.to_thread.run_sync(copy_object_to, key, out)


def generate_presigned_get(
    key: str,
    *,
    expires_in: int = 300,
    download_filename: str | None = None,
) -> str:
    """Return a short-lived GET URL for an S3 object.

    When ``download_filename`` is provided the URL carries an
    ``attachment`` Content-Disposition. S3 honours the client-supplied
    ``response-content-disposition`` query parameter, overriding
    whatever Content-Type the object was stored with. This is how we
    stop a user-uploaded ``.html`` (or renamed ``image/*`` that's
    really HTML) from being rendered inline in the browser — every
    attachment is forced to download. Inline image previews should use
    the separate ``/thumbnail`` endpoint which serves our server-
    generated JPG instead of the raw file.
    """
    settings = get_settings()
    params: dict[str, object] = {"Bucket": settings.s3_bucket, "Key": key}
    if download_filename:
        # ASCII-safe filename plus a UTF-8 RFC 5987 fallback so non-Latin
        # names survive without corrupting the header encoding.
        ascii_name = (
            download_filename.encode("ascii", errors="replace").decode("ascii").replace('"', "_")
        )
        from urllib.parse import quote

        utf8_name = quote(download_filename, safe="")
        params["ResponseContentDisposition"] = (
            f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}"
        )
    # IMPORTANT: use the public client so the returned URL contains a host
    # the browser can reach. In docker-compose the internal endpoint is
    # `http://minio:9000` which is unreachable from outside.
    return get_public_s3_client().generate_presigned_url(
        "get_object",
        Params=params,
        ExpiresIn=expires_in,
    )
