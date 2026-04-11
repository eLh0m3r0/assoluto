"""End-to-end attachment tests using `moto` to mock S3.

Covers: contact uploads an image -> row is stored with correct
storage_key -> thumbnail background task renders a JPEG -> download
redirect returns a presigned URL -> staff and contact access control
-> oversize upload rejected -> contact locked out after submit.
"""

from __future__ import annotations

import io
from datetime import datetime
from uuid import UUID, uuid4

import boto3
import pytest
from httpx import AsyncClient
from moto import mock_aws
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.attachment import OrderAttachment
from app.models.customer import Customer, CustomerContact
from app.models.enums import CustomerContactRole, UserRole
from app.models.user import User
from app.security.passwords import hash_password

pytestmark = pytest.mark.postgres


# ---------- moto fixture --------------------------------------------------


@pytest.fixture
def mock_s3(settings):
    """Start a moto mock S3 and create the configured bucket.

    The fixture clears the `get_s3_client` cache so the app talks to the
    in-process moto backend, then restores and clears again on teardown.
    """
    from app.storage import s3 as s3_mod

    with mock_aws():
        # Clear any cached client from other tests.
        s3_mod.get_s3_client.cache_clear()

        # Create the bucket the settings point at.
        client = boto3.client(
            "s3",
            endpoint_url=None,  # moto intercepts AWS endpoints
            aws_access_key_id="test",
            aws_secret_access_key="test",
            region_name="eu-central-1",
        )
        client.create_bucket(
            Bucket=settings.s3_bucket,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )
        yield
        s3_mod.get_s3_client.cache_clear()


@pytest.fixture(autouse=True)
def _point_settings_at_moto(monkeypatch):
    """Make the app's boto3 client hit moto instead of a real MinIO."""
    monkeypatch.setenv("S3_ENDPOINT_URL", "")
    monkeypatch.setenv("S3_ACCESS_KEY", "test")
    monkeypatch.setenv("S3_SECRET_KEY", "test")
    monkeypatch.setenv("S3_BUCKET", "portal-test")
    monkeypatch.setenv("S3_REGION", "eu-central-1")
    monkeypatch.setenv("S3_USE_SSL", "false")
    yield


# ---------- helpers -------------------------------------------------------


async def _seed_everyone(owner_engine, tenant_id: UUID) -> dict:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        staff = User(
            id=uuid4(),
            tenant_id=tenant_id,
            email="staff@4mex.cz",
            full_name="Staff",
            role=UserRole.TENANT_ADMIN,
            password_hash=hash_password("staffpass"),
        )
        customer = Customer(id=uuid4(), tenant_id=tenant_id, name="ACME", ico="11111111")
        session.add_all([staff, customer])
        await session.flush()
        contact = CustomerContact(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=customer.id,
            email="jan@acme.cz",
            full_name="Jan",
            role=CustomerContactRole.CUSTOMER_ADMIN,
            password_hash=hash_password("contactpass"),
            invited_at=datetime.now(),
            accepted_at=datetime.now(),
        )
        session.add(contact)
        await session.flush()
        return {"staff": staff, "customer": customer, "contact": contact}


async def _login(client: AsyncClient, email: str, password: str) -> None:
    resp = await client.post(
        "/auth/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


def _png_bytes() -> bytes:
    """Return a tiny valid PNG for upload tests."""
    buf = io.BytesIO()
    Image.new("RGB", (50, 50), color=(200, 100, 50)).save(buf, format="PNG")
    return buf.getvalue()


# ---------- tests ---------------------------------------------------------


async def test_contact_uploads_image_and_thumbnail_is_generated(
    tenant_client: AsyncClient, owner_engine, demo_tenant, mock_s3
) -> None:
    await _seed_everyone(owner_engine, demo_tenant.id)

    # Contact logs in, creates a DRAFT order.
    await _login(tenant_client, "jan@acme.cz", "contactpass")
    create_resp = await tenant_client.post(
        "/app/orders", data={"title": "S přílohou"}, follow_redirects=False
    )
    order_id = UUID(create_resp.headers["location"].rsplit("/", 1)[-1])

    # Upload a PNG.
    png = _png_bytes()
    upload_resp = await tenant_client.post(
        f"/app/orders/{order_id}/attachments",
        files={"file": ("vykres.png", png, "image/png")},
        follow_redirects=False,
    )
    assert upload_resp.status_code == 303

    # DB row should exist; thumbnail task runs as background task so by the
    # time the ASGI response lifecycle is complete, the thumbnail_key is set.
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        attachments = (
            (
                await session.execute(
                    select(OrderAttachment).where(OrderAttachment.order_id == order_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(attachments) == 1
    att = attachments[0]
    assert att.filename == "vykres.png"
    assert att.content_type == "image/png"
    assert att.size_bytes == len(png)
    assert att.storage_key.startswith("tenants/4mex/")
    assert att.storage_key.endswith(f"attachments/{att.id}/vykres.png")
    assert att.thumbnail_key is not None
    assert att.thumbnail_key.endswith(f"thumbnails/{att.id}.jpg")

    # Detail page shows the attachment row.
    detail = await tenant_client.get(f"/app/orders/{order_id}")
    assert "vykres.png" in detail.text

    # Download route redirects to a presigned URL.
    download = await tenant_client.get(
        f"/app/attachments/{att.id}/download", follow_redirects=False
    )
    assert download.status_code == 302
    assert (
        "Signature=" in download.headers["location"]
        or "X-Amz-Signature" in download.headers["location"]
    )


async def test_oversize_upload_is_rejected(
    tenant_client: AsyncClient, owner_engine, demo_tenant, mock_s3
) -> None:
    await _seed_everyone(owner_engine, demo_tenant.id)

    # `Settings` already populated when tenant_client was created. Shrink
    # the limit directly on the live settings object (pydantic settings
    # are mutable unless frozen).
    tenant_client._transport.app.state.settings.max_upload_size_mb = 1  # type: ignore[attr-defined]

    await _login(tenant_client, "jan@acme.cz", "contactpass")
    create_resp = await tenant_client.post(
        "/app/orders", data={"title": "Oversize"}, follow_redirects=False
    )
    order_id = UUID(create_resp.headers["location"].rsplit("/", 1)[-1])

    big = b"x" * (2 * 1024 * 1024)
    upload_resp = await tenant_client.post(
        f"/app/orders/{order_id}/attachments",
        files={"file": ("big.bin", big, "application/octet-stream")},
        follow_redirects=False,
    )
    assert upload_resp.status_code == 413


async def test_cross_customer_attachment_is_hidden(
    tenant_client: AsyncClient, owner_engine, demo_tenant, mock_s3
) -> None:
    await _seed_everyone(owner_engine, demo_tenant.id)

    # Jan creates order + uploads attachment.
    await _login(tenant_client, "jan@acme.cz", "contactpass")
    create_resp = await tenant_client.post(
        "/app/orders", data={"title": "ACME only"}, follow_redirects=False
    )
    order_id = UUID(create_resp.headers["location"].rsplit("/", 1)[-1])
    upload_resp = await tenant_client.post(
        f"/app/orders/{order_id}/attachments",
        files={"file": ("a.png", _png_bytes(), "image/png")},
        follow_redirects=False,
    )
    assert upload_resp.status_code == 303

    # Grab the attachment id.
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        att = (
            await session.execute(
                select(OrderAttachment).where(OrderAttachment.order_id == order_id)
            )
        ).scalar_one()

    # Create a second customer + contact, log in as them, try to download.
    async with sm() as session, session.begin():
        other = Customer(id=uuid4(), tenant_id=demo_tenant.id, name="Other", ico="99999999")
        session.add(other)
        await session.flush()
        session.add(
            CustomerContact(
                id=uuid4(),
                tenant_id=demo_tenant.id,
                customer_id=other.id,
                email="eva@other.cz",
                full_name="Eva",
                role=CustomerContactRole.CUSTOMER_USER,
                password_hash=hash_password("evapass"),
                invited_at=datetime.now(),
                accepted_at=datetime.now(),
            )
        )
        await session.flush()

    # Logout Jan, login Eva.
    await tenant_client.post("/auth/logout", follow_redirects=False)
    tenant_client.cookies.clear()
    await _login(tenant_client, "eva@other.cz", "evapass")

    resp = await tenant_client.get(f"/app/attachments/{att.id}/download", follow_redirects=False)
    assert resp.status_code == 404


async def test_generate_thumbnail_directly(
    tenant_client: AsyncClient, owner_engine, demo_tenant, mock_s3
) -> None:
    """Bypass BackgroundTasks and call the task coroutine directly.

    Proves the task body itself works end-to-end against the mocked
    S3; any BackgroundTasks timing issues in the wrapper test are a
    separate concern.
    """
    await _seed_everyone(owner_engine, demo_tenant.id)

    await _login(tenant_client, "jan@acme.cz", "contactpass")
    create_resp = await tenant_client.post(
        "/app/orders", data={"title": "Direct thumb"}, follow_redirects=False
    )
    order_id = UUID(create_resp.headers["location"].rsplit("/", 1)[-1])

    upload_resp = await tenant_client.post(
        f"/app/orders/{order_id}/attachments",
        files={"file": ("direct.png", _png_bytes(), "image/png")},
        follow_redirects=False,
    )
    assert upload_resp.status_code == 303

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        att = (
            await session.execute(
                select(OrderAttachment).where(OrderAttachment.order_id == order_id)
            )
        ).scalar_one()

    # Force a fresh sessionmaker so the task opens its own transaction.
    from app.db import session as db_session
    from app.tasks.thumbnail_tasks import generate_thumbnail

    db_session.get_engine.cache_clear()
    db_session.get_sessionmaker.cache_clear()

    await generate_thumbnail(att.id, demo_tenant.id)

    async with sm() as session:
        refreshed = (
            await session.execute(select(OrderAttachment).where(OrderAttachment.id == att.id))
        ).scalar_one()
    assert refreshed.thumbnail_key is not None
    assert refreshed.thumbnail_key.endswith(f"{att.id}.jpg")


async def test_contact_cannot_delete_after_submit(
    tenant_client: AsyncClient, owner_engine, demo_tenant, mock_s3
) -> None:
    await _seed_everyone(owner_engine, demo_tenant.id)

    await _login(tenant_client, "jan@acme.cz", "contactpass")
    create_resp = await tenant_client.post(
        "/app/orders", data={"title": "Locked"}, follow_redirects=False
    )
    order_id = UUID(create_resp.headers["location"].rsplit("/", 1)[-1])

    await tenant_client.post(
        f"/app/orders/{order_id}/attachments",
        files={"file": ("a.png", _png_bytes(), "image/png")},
        follow_redirects=False,
    )

    # Submit the order.
    await tenant_client.post(
        f"/app/orders/{order_id}/transitions/submitted", follow_redirects=False
    )

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        att = (
            await session.execute(
                select(OrderAttachment).where(OrderAttachment.order_id == order_id)
            )
        ).scalar_one()

    # Contact tries to delete -> 409.
    delete_resp = await tenant_client.post(
        f"/app/attachments/{att.id}/delete", follow_redirects=False
    )
    assert delete_resp.status_code == 409
