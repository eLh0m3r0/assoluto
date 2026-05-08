"""S3 / MinIO client helper.

Keeps boto3 configuration in one place so the rest of the app only
touches a thin wrapper. Used by the attachment upload route (put_object)
and the background thumbnail task (get_object + put_object).
"""

from __future__ import annotations

from functools import lru_cache
from io import BytesIO

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
