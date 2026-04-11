"""Attachment routes: upload (multipart), download, delete.

The current flow is multipart-server-side: the client POSTs the file
to FastAPI, which streams it to S3. This is simpler to test and
sufficient for MVP (files up to MAX_UPLOAD_SIZE_MB). Direct-to-S3
presigned uploads land in roadmap item R0 together with the queue
migration.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import RedirectResponse, Response

from app.deps import Principal, get_db, require_login
from app.security.csrf import verify_csrf
from app.services.attachment_service import (
    AttachmentError,
    AttachmentTooLarge,
    create_attachment_row,
    delete_attachment,
    get_attachment,
)
from app.services.order_service import (
    ActorRef,
    OrderAccessDenied,
    OrderNotFound,
    get_order_for_principal,
)
from app.storage import s3 as s3_storage
from app.tasks.thumbnail_tasks import generate_thumbnail

router = APIRouter(prefix="/app", tags=["attachments"], dependencies=[Depends(verify_csrf)])


def _actor(principal: Principal) -> ActorRef:
    return ActorRef(
        type=principal.type,
        id=principal.id,
        customer_id=principal.customer_id,
    )


@router.post(
    "/orders/{order_id}/attachments",
    include_in_schema=False,
)
async def upload_attachment(
    order_id: UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    order_item_id: str = Form(""),
    principal: Principal = Depends(require_login),
    db=Depends(get_db),
) -> Response:
    # Read settings off request.state so tests can tweak limits on the
    # live app without juggling the lru_cache.
    settings = request.app.state.settings
    try:
        order = await get_order_for_principal(db, order_id=order_id, actor=_actor(principal))
    except (OrderNotFound, OrderAccessDenied):
        raise HTTPException(status_code=404, detail="Order not found") from None

    # Read body into memory (MVP). For large files a streaming upload will
    # replace this in R0 alongside presigned PUTs.
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    item_uuid: UUID | None = None
    if order_item_id:
        try:
            item_uuid = UUID(order_item_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid order_item_id") from None

    tenant = request.state.tenant
    try:
        attachment = await create_attachment_row(
            db,
            tenant=tenant,
            order=order,
            filename=file.filename or "upload.bin",
            content_type=file.content_type or "application/octet-stream",
            size_bytes=len(data),
            max_size_bytes=settings.max_upload_size_bytes,
            order_item_id=item_uuid,
            uploaded_by_user_id=principal.id if principal.is_staff else None,
            uploaded_by_contact_id=principal.id if not principal.is_staff else None,
        )
    except AttachmentTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from None
    except AttachmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    s3_storage.upload_bytes(attachment.storage_key, data, content_type=attachment.content_type)

    # Snapshot IDs before committing — after commit the ORM instance may be
    # expired and touching it would fire a fresh query.
    att_id = attachment.id
    tenant_id = tenant.id

    # IMPORTANT: commit BEFORE registering the background task. FastAPI runs
    # background tasks inside `await response(send)`, which executes before
    # the request-scoped dependency stack (and therefore `get_db`'s cleanup)
    # has a chance to commit. Without this explicit commit, the background
    # task opens a new connection and sees no row yet.
    await db.commit()

    background_tasks.add_task(generate_thumbnail, att_id, tenant_id)

    return RedirectResponse(url=f"/app/orders/{order.id}", status_code=303)


@router.get("/attachments/{attachment_id}/download")
async def download_attachment(
    attachment_id: UUID,
    request: Request,
    principal: Principal = Depends(require_login),
    db=Depends(get_db),
) -> Response:
    attachment = await get_attachment(db, attachment_id)
    if attachment is None:
        raise HTTPException(status_code=404, detail="Attachment not found")

    try:
        await get_order_for_principal(db, order_id=attachment.order_id, actor=_actor(principal))
    except (OrderNotFound, OrderAccessDenied):
        raise HTTPException(status_code=404, detail="Attachment not found") from None

    url = s3_storage.generate_presigned_get(attachment.storage_key)
    return RedirectResponse(url=url, status_code=302)


@router.get("/attachments/{attachment_id}/thumbnail")
async def thumbnail_redirect(
    attachment_id: UUID,
    request: Request,
    principal: Principal = Depends(require_login),
    db=Depends(get_db),
) -> Response:
    attachment = await get_attachment(db, attachment_id)
    if attachment is None or attachment.thumbnail_key is None:
        raise HTTPException(status_code=404, detail="No thumbnail")
    try:
        await get_order_for_principal(db, order_id=attachment.order_id, actor=_actor(principal))
    except (OrderNotFound, OrderAccessDenied):
        raise HTTPException(status_code=404, detail="No thumbnail") from None

    url = s3_storage.generate_presigned_get(attachment.thumbnail_key)
    return RedirectResponse(url=url, status_code=302)


@router.post("/attachments/{attachment_id}/delete")
async def delete_attachment_route(
    attachment_id: UUID,
    request: Request,
    principal: Principal = Depends(require_login),
    db=Depends(get_db),
) -> Response:
    attachment = await get_attachment(db, attachment_id)
    if attachment is None:
        raise HTTPException(status_code=404, detail="Attachment not found")

    try:
        order = await get_order_for_principal(
            db, order_id=attachment.order_id, actor=_actor(principal)
        )
    except (OrderNotFound, OrderAccessDenied):
        raise HTTPException(status_code=404, detail="Attachment not found") from None

    # Contacts can only delete attachments on DRAFT orders.
    if not principal.is_staff and order.status.value != "draft":
        raise HTTPException(status_code=409, detail="Attachments are locked")

    # Best-effort S3 cleanup; the row delete is the source of truth.
    try:
        s3_storage.delete_object(attachment.storage_key)
        if attachment.thumbnail_key:
            s3_storage.delete_object(attachment.thumbnail_key)
    except Exception:
        pass

    await delete_attachment(db, attachment)
    return RedirectResponse(url=f"/app/orders/{order.id}", status_code=303)
