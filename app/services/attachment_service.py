"""Attachment service: key generation, DB creation, deletion."""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attachment import OrderAttachment
from app.models.enums import AttachmentKind
from app.models.order import Order
from app.models.tenant import Tenant


class AttachmentError(Exception):
    pass


class AttachmentTooLarge(AttachmentError):
    pass


ALLOWED_CONTENT_TYPES: set[str] = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/webp",
    # CAD formats — stored as opaque blobs, no thumbnail generation.
    "application/acad",
    "image/vnd.dwg",
    "application/dxf",
    "image/vnd.dxf",
    "application/octet-stream",  # fallback for DWG/DXF uploads
}


def _detect_kind(content_type: str, filename: str) -> AttachmentKind:
    name = filename.lower()
    if content_type.startswith("image/"):
        return AttachmentKind.PHOTO
    if name.endswith((".dwg", ".dxf", ".step", ".stp", ".stl", ".iges", ".igs")):
        return AttachmentKind.DRAWING
    if content_type == "application/pdf":
        return AttachmentKind.DOCUMENT
    return AttachmentKind.OTHER


def build_storage_key(
    *,
    tenant: Tenant,
    order_id: UUID,
    attachment_id: UUID,
    filename: str,
) -> str:
    """Return the canonical S3 key for an attachment.

    We embed the attachment UUID to avoid filename collisions and path
    traversal (client cannot choose the key). The raw filename is only
    kept in the DB row for display and Content-Disposition headers.
    """
    safe_name = filename.rsplit("/", 1)[-1].replace("\\", "_") or "file"
    return (
        f"{tenant.storage_prefix.rstrip('/')}/orders/{order_id}/attachments/"
        f"{attachment_id}/{safe_name}"
    )


def build_thumbnail_key(*, tenant: Tenant, order_id: UUID, attachment_id: UUID) -> str:
    return f"{tenant.storage_prefix.rstrip('/')}/orders/{order_id}/thumbnails/{attachment_id}.jpg"


async def create_attachment_row(
    db: AsyncSession,
    *,
    tenant: Tenant,
    order: Order,
    filename: str,
    content_type: str,
    size_bytes: int,
    max_size_bytes: int,
    order_item_id: UUID | None = None,
    uploaded_by_user_id: UUID | None = None,
    uploaded_by_contact_id: UUID | None = None,
) -> OrderAttachment:
    """Insert an OrderAttachment row BEFORE the upload occurs.

    Returns the attachment so the caller can generate the presigned URL
    and hand the key back to the client.
    """
    if size_bytes <= 0:
        raise AttachmentError("size_bytes must be positive")
    if size_bytes > max_size_bytes:
        raise AttachmentTooLarge(f"file exceeds max size of {max_size_bytes} bytes")

    # Plan storage quota — 2 GB on Starter, 20 GB on Pro, unlimited on
    # community / Enterprise. Ceil to the next full MB so a 512-byte
    # file still counts as 1 MB of pressure (cheap safety margin).
    from app.platform.usage import ensure_within_limit

    size_mb = max(1, (size_bytes + 1024 * 1024 - 1) // (1024 * 1024))
    await ensure_within_limit(
        db, tenant_id=tenant.id, metric="storage_mb", delta=size_mb
    )

    attachment = OrderAttachment(
        id=uuid4(),
        tenant_id=tenant.id,
        order_id=order.id,
        order_item_id=order_item_id,
        kind=_detect_kind(content_type, filename),
        filename=filename.rsplit("/", 1)[-1][:255],
        content_type=content_type or "application/octet-stream",
        size_bytes=size_bytes,
        storage_key="",  # set below once id is fixed
        uploaded_by_user_id=uploaded_by_user_id,
        uploaded_by_contact_id=uploaded_by_contact_id,
    )
    attachment.storage_key = build_storage_key(
        tenant=tenant,
        order_id=order.id,
        attachment_id=attachment.id,
        filename=attachment.filename,
    )
    db.add(attachment)
    await db.flush()
    return attachment


async def list_for_order(db: AsyncSession, order_id: UUID) -> list[OrderAttachment]:
    result = await db.execute(
        select(OrderAttachment)
        .where(OrderAttachment.order_id == order_id)
        .order_by(OrderAttachment.created_at)
    )
    return list(result.scalars().all())


async def get_attachment(db: AsyncSession, attachment_id: UUID) -> OrderAttachment | None:
    return (
        await db.execute(select(OrderAttachment).where(OrderAttachment.id == attachment_id))
    ).scalar_one_or_none()


async def delete_attachment(db: AsyncSession, attachment: OrderAttachment) -> None:
    await db.delete(attachment)
    await db.flush()
