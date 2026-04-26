"""Background task: generate a small JPEG preview for an attachment.

Runs from FastAPI BackgroundTasks so clients see immediate upload
response; the thumbnail shows up a few seconds later.
"""

from __future__ import annotations

from io import BytesIO
from uuid import UUID

from sqlalchemy import select

from app.db.session import get_sessionmaker
from app.deps import set_tenant_context
from app.logging import get_logger
from app.models.attachment import OrderAttachment
from app.services.attachment_service import build_thumbnail_key
from app.storage import s3 as s3_storage

log = get_logger("app.tasks.thumbnail")

THUMBNAIL_MAX_SIZE = (400, 400)


def _render_thumbnail(data: bytes, content_type: str) -> bytes | None:
    """Return JPEG bytes or None if the content type can't be previewed."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a hard dep in prod
        return None

    if content_type.startswith("image/"):
        try:
            with Image.open(BytesIO(data)) as img:
                img.thumbnail(THUMBNAIL_MAX_SIZE)
                # ``Image.open`` returns ``ImageFile`` (a subclass of
                # ``Image``); ``.convert`` returns a fresh ``Image``.
                # Re-binding loses the ImageFile-only attributes but we
                # don't use any of them past this point.
                rgb_img: Image.Image = img if img.mode in ("RGB", "L") else img.convert("RGB")
                out = BytesIO()
                rgb_img.save(out, format="JPEG", quality=80)
                return out.getvalue()
        except Exception as exc:
            log.warning("thumbnail.image_failed", error=str(exc))
            return None

    if content_type == "application/pdf":
        try:
            from pdf2image import convert_from_bytes
        except ImportError:
            return None
        try:
            pages = convert_from_bytes(data, first_page=1, last_page=1, size=400)
            if not pages:
                return None
            out = BytesIO()
            pages[0].save(out, format="JPEG", quality=80)
            return out.getvalue()
        except Exception as exc:
            log.warning("thumbnail.pdf_failed", error=str(exc))
            return None

    return None


async def generate_thumbnail(attachment_id: UUID, tenant_id: UUID) -> None:
    """Fetch the object from S3, render a JPEG thumbnail, upload it back.

    Opens its own tenant-scoped DB session so the caller's request
    transaction doesn't need to stay alive. Uploader endpoints MUST
    `await db.commit()` before scheduling this task — see
    `app.routers.attachments.upload_attachment` for the rationale.
    """
    log.info("thumbnail.start", id=str(attachment_id))
    sm = get_sessionmaker()
    try:
        async with sm() as session, session.begin():
            await set_tenant_context(session, str(tenant_id))
            attachment = (
                await session.execute(
                    select(OrderAttachment).where(OrderAttachment.id == attachment_id)
                )
            ).scalar_one_or_none()
            if attachment is None:
                log.info(
                    "thumbnail.skip",
                    reason="attachment gone",
                    id=str(attachment_id),
                )
                return

            try:
                data = s3_storage.download_bytes(attachment.storage_key)
            except Exception as exc:
                log.warning(
                    "thumbnail.download_failed",
                    id=str(attachment_id),
                    error=str(exc),
                )
                return

            jpeg = _render_thumbnail(data, attachment.content_type)
            if jpeg is None:
                log.info(
                    "thumbnail.unsupported",
                    id=str(attachment_id),
                    content_type=attachment.content_type,
                )
                return

            from app.models.tenant import Tenant

            tenant = (
                await session.execute(select(Tenant).where(Tenant.id == tenant_id))
            ).scalar_one_or_none()
            if tenant is None:
                return

            thumb_key = build_thumbnail_key(
                tenant=tenant,
                order_id=attachment.order_id,
                attachment_id=attachment.id,
            )
            try:
                s3_storage.upload_bytes(thumb_key, jpeg, content_type="image/jpeg")
            except Exception as exc:
                log.warning("thumbnail.upload_failed", error=str(exc))
                return

            attachment.thumbnail_key = thumb_key
            await session.flush()
            log.info("thumbnail.generated", id=str(attachment_id), key=thumb_key)
    except Exception as exc:
        log.error(
            "thumbnail.fatal",
            id=str(attachment_id),
            error=f"{type(exc).__name__}: {exc}",
        )
