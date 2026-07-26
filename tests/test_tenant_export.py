"""Whole-tenant ZIP export.

The Terms, the pricing page, the privacy policy and the homepage FAQ all
promised "orders and customers as CSV, attachments as ZIP" and nothing
implemented it (audit 2026-07-26). These tests pin the promise.
"""

from __future__ import annotations

import csv
import io
import zipfile
from decimal import Decimal
from uuid import uuid4

import boto3
import pytest
from moto import mock_aws
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.attachment import OrderAttachment
from app.models.customer import Customer, CustomerContact
from app.models.enums import AttachmentKind, UserRole
from app.models.order import Order, OrderItem
from app.models.user import User
from app.security.passwords import hash_password
from app.services.tenant_export_service import build_tenant_export

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def _point_settings_at_moto(monkeypatch):
    monkeypatch.setenv("S3_ENDPOINT_URL", "")
    monkeypatch.setenv("S3_ACCESS_KEY", "test")
    monkeypatch.setenv("S3_SECRET_KEY", "test")
    monkeypatch.setenv("S3_BUCKET", "portal-test")
    monkeypatch.setenv("S3_REGION", "eu-central-1")
    monkeypatch.setenv("S3_USE_SSL", "false")
    yield


@pytest.fixture
def mock_s3(settings):
    """In-process moto S3 with the configured bucket pre-created."""
    from app.storage import s3 as s3_mod

    with mock_aws():
        s3_mod.get_s3_client.cache_clear()
        client = boto3.client(
            "s3",
            endpoint_url=None,
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


def _read(zf: zipfile.ZipFile, name: str) -> list[dict]:
    text = zf.read(name).decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


async def test_export_contains_business_records_and_attachment_bytes(
    owner_engine, demo_tenant, mock_s3
) -> None:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        user = User(
            id=uuid4(),
            tenant_id=demo_tenant.id,
            email=f"exp-{uuid4().hex[:6]}@4mex.cz",
            full_name="Exporter",
            role=UserRole.TENANT_ADMIN,
            password_hash=hash_password("x"),
        )
        cust = Customer(id=uuid4(), tenant_id=demo_tenant.id, name="Exportní zákazník s.r.o.")
        session.add_all([user, cust])
        await session.flush()
        contact = CustomerContact(
            id=uuid4(),
            tenant_id=demo_tenant.id,
            customer_id=cust.id,
            email="martina@export.cz",
            full_name="Martina Nováková",
            password_hash=hash_password("x"),
        )
        order = Order(
            id=uuid4(),
            tenant_id=demo_tenant.id,
            customer_id=cust.id,
            number="2026-EXP-0001",
            title="Frézování příruby",
        )
        session.add_all([contact, order])
        await session.flush()
        session.add(
            OrderItem(
                id=uuid4(),
                tenant_id=demo_tenant.id,
                order_id=order.id,
                description="Příruba DN50",
                quantity=Decimal("4"),
                unit="ks",
                unit_price=Decimal("250"),
                line_total=Decimal("1000"),
                position=1,
            )
        )

        # An attachment whose bytes really live in (mocked) S3.
        from app.storage.s3 import upload_bytes

        key = f"{demo_tenant.slug}/orders/{order.id}/vykres.pdf"
        upload_bytes(key, b"%PDF-1.4 fake drawing bytes", content_type="application/pdf")
        session.add(
            OrderAttachment(
                id=uuid4(),
                tenant_id=demo_tenant.id,
                order_id=order.id,
                kind=AttachmentKind.DRAWING,
                filename="vykres.pdf",
                content_type="application/pdf",
                size_bytes=27,
                storage_key=key,
            )
        )

    async with sm() as session:
        blob = await build_tenant_export(session, tenant_slug=demo_tenant.slug)

    zf = zipfile.ZipFile(io.BytesIO(blob))
    names = set(zf.namelist())

    # The promise, literally: orders and customers as CSV.
    assert "orders.csv" in names
    assert "customers.csv" in names
    assert "customer_contacts.csv" in names
    assert "order_items.csv" in names
    assert "README.txt" in names

    orders = _read(zf, "orders.csv")
    assert any(o["number"] == "2026-EXP-0001" for o in orders)

    customers = _read(zf, "customers.csv")
    # Round-trips Czech text — the CSVs carry a BOM so Excel gets it right.
    assert any(c["name"] == "Exportní zákazník s.r.o." for c in customers)

    contacts = _read(zf, "customer_contacts.csv")
    assert any(c["email"] == "martina@export.cz" for c in contacts)

    # ...and attachments as real bytes, not just metadata.
    att_names = [n for n in names if n.startswith("attachments/")]
    assert att_names == ["attachments/2026-EXP-0001/vykres.pdf"]
    assert zf.read(att_names[0]) == b"%PDF-1.4 fake drawing bytes"

    # A clean export must not claim errors it did not hit.
    assert "_export_errors.txt" not in names


async def test_export_survives_a_missing_storage_object(
    owner_engine, demo_tenant, mock_s3
) -> None:
    """A dangling storage_key must degrade to a note, not a 500.

    A departing customer with one unreadable file still needs the other
    nine years of records.
    """
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        cust = Customer(id=uuid4(), tenant_id=demo_tenant.id, name="Dangling")
        session.add(cust)
        await session.flush()
        order = Order(
            id=uuid4(),
            tenant_id=demo_tenant.id,
            customer_id=cust.id,
            number="2026-EXP-0002",
            title="Chybějící soubor",
        )
        session.add(order)
        await session.flush()
        session.add(
            OrderAttachment(
                id=uuid4(),
                tenant_id=demo_tenant.id,
                order_id=order.id,
                kind=AttachmentKind.DRAWING,
                filename="gone.pdf",
                content_type="application/pdf",
                size_bytes=1,
                storage_key="does/not/exist.pdf",
            )
        )

    async with sm() as session:
        blob = await build_tenant_export(session, tenant_slug=demo_tenant.slug)

    zf = zipfile.ZipFile(io.BytesIO(blob))
    assert "orders.csv" in zf.namelist()
    assert "_export_errors.txt" in zf.namelist()
    assert "gone.pdf" in zf.read("_export_errors.txt").decode("utf-8")


async def test_export_route_requires_tenant_admin(tenant_client, demo_tenant) -> None:
    """Every order, customer and drawing in the account is a higher bar
    than the per-person GDPR export.
    """
    resp = await tenant_client.get("/app/admin/export")
    # Unauthenticated -> auth redirect / 401, never a ZIP.
    assert resp.status_code != 200
    assert "application/zip" not in resp.headers.get("content-type", "")
