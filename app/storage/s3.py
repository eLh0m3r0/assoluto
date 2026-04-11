"""S3 / MinIO client helper.

Keeps boto3 configuration in one place so the rest of the app only
touches a thin wrapper. Used by both the attachment upload route
(presigned PUT URLs) and the background thumbnail task (direct GET/PUT).
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
    """Return a cached boto3 S3 client configured for the current env.

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


def ensure_bucket_exists(bucket: str | None = None) -> None:
    """Create the application bucket if it doesn't already exist.

    Called at app startup so MinIO-on-localhost doesn't require manual setup.
    Swallows `BucketAlreadyOwnedByYou`; re-raises anything else.
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
    """Upload raw bytes under `key` (synchronous — used by thumbnail tasks)."""
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
    return get_s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": key},
        ExpiresIn=expires_in,
    )


def generate_presigned_put(
    key: str,
    *,
    content_type: str,
    content_length: int,
    expires_in: int = 900,
) -> str:
    """Return a presigned PUT URL for direct-to-S3 uploads.

    The `Content-Length` and `Content-Type` are fixed into the signature,
    so clients cannot use the URL to upload larger or different files.
    """
    settings = get_settings()
    return get_s3_client().generate_presigned_url(
        "put_object",
        Params={
            "Bucket": settings.s3_bucket,
            "Key": key,
            "ContentType": content_type,
            "ContentLength": content_length,
        },
        ExpiresIn=expires_in,
    )


def head_object_size(key: str) -> int | None:
    """Return the object's size in bytes, or None if it doesn't exist yet."""
    settings = get_settings()
    try:
        response = get_s3_client().head_object(Bucket=settings.s3_bucket, Key=key)
        return int(response.get("ContentLength", 0))
    except ClientError:
        return None
