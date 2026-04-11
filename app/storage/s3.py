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


def generate_presigned_get(key: str, *, expires_in: int = 300) -> str:
    settings = get_settings()
    # IMPORTANT: use the public client so the returned URL contains a host
    # the browser can reach. In docker-compose the internal endpoint is
    # `http://minio:9000` which is unreachable from outside.
    return get_public_s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": key},
        ExpiresIn=expires_in,
    )
