"""Whole-tenant data export — "take your data and leave".

The marketing FAQ, the pricing page, the privacy policy and — the part
that actually matters — ``terms.html`` §"The Customer is responsible for
maintaining their own data exports" all promised *"The CSV / ZIP export
endpoints are available at any time during the active subscription"*,
with the homepage spelling it out as "orders and customers as CSV,
attachments as ZIP".

None of it existed. ``grep -riE "zipfile|ZipFile" app/ scripts/``
returned nothing, and the only bulk export in the product was the order
*header* CSV on the orders list. A cancelling customer had three days to
use an endpoint that was never built, against a promise sitting in a
binding contract.

This module builds the thing the Terms describe: one ZIP containing the
tenant's business records as CSV plus every uploaded file.

Design notes
------------
* Runs under the caller's RLS-scoped session, so it can only ever see
  the caller's own tenant. There is no tenant_id parameter to get wrong.
* Writes into a ``SpooledTemporaryFile`` (RAM up to 8 MB, then disk) and
  the router streams that file back in 1 MB chunks. Attachment bodies are
  copied from S3 chunk by chunk in a worker thread, so peak memory is a
  few MB regardless of how large the tenant's storage is (audit BE-16).
* Attachment bytes are best-effort: a missing S3 object records a line
  in ``_export_errors.txt`` rather than failing the whole export. A
  partial archive is worth far more to a departing customer than a 500.
"""

from __future__ import annotations

import csv
import io
import tempfile
import zipfile
from datetime import UTC, datetime
from typing import IO, Any

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging import get_logger
from app.models.asset import Asset, AssetMovement
from app.models.attachment import OrderAttachment
from app.models.customer import Customer, CustomerContact
from app.models.order import Order, OrderComment, OrderItem, OrderStatusHistory
from app.models.product import Product
from app.models.user import User

log = get_logger("app.export")

# Belt and braces against a pathological tenant turning an export into an
# OOM. Anything beyond this is a support conversation, not a click.
MAX_ATTACHMENT_BYTES = 512 * 1024 * 1024


def _rows_to_csv(rows: list[Any], columns: list[str]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    for row in rows:
        writer.writerow(["" if getattr(row, c, None) is None else getattr(row, c) for c in columns])
    return buf.getvalue().encode("utf-8-sig")  # BOM: Excel opens CS/DE text correctly


_TABLES: list[tuple[str, Any, list[str]]] = [
    (
        "orders",
        Order,
        [
            "id",
            "number",
            "title",
            "status",
            "customer_id",
            "currency",
            "quoted_total",
            "notes",
            "requested_delivery_at",
            "promised_delivery_at",
            "submitted_at",
            "delivered_at",
            "closed_at",
            "created_at",
            "updated_at",
        ],
    ),
    (
        "order_items",
        OrderItem,
        [
            "id",
            "order_id",
            "product_id",
            "description",
            "quantity",
            "unit",
            "unit_price",
            "line_total",
            "position",
            "created_at",
        ],
    ),
    (
        "order_comments",
        OrderComment,
        [
            "id",
            "order_id",
            "body",
            "is_internal",
            "author_user_id",
            "author_contact_id",
            "created_at",
        ],
    ),
    (
        "order_status_history",
        OrderStatusHistory,
        [
            "id",
            "order_id",
            "from_status",
            "to_status",
            "note",
            "changed_by_user_id",
            "changed_by_contact_id",
            "created_at",
        ],
    ),
    (
        "customers",
        Customer,
        ["id", "name", "ico", "dic", "notes", "preferred_locale", "created_at"],
    ),
    (
        "customer_contacts",
        CustomerContact,
        [
            "id",
            "customer_id",
            "email",
            "full_name",
            "is_active",
            "invited_at",
            "accepted_at",
            "last_login_at",
            "created_at",
        ],
    ),
    (
        "products",
        Product,
        ["id", "sku", "name", "unit", "default_price", "customer_id", "is_active", "created_at"],
    ),
    ("users", User, ["id", "email", "full_name", "role", "is_active", "created_at"]),
    (
        "assets",
        Asset,
        [
            "id",
            "customer_id",
            "name",
            "code",
            "description",
            "unit",
            "current_quantity",
            "location",
            "is_active",
            "created_at",
        ],
    ),
    (
        "asset_movements",
        AssetMovement,
        [
            "id",
            "asset_id",
            "type",
            "quantity",
            "note",
            "occurred_at",
            "reference_order_id",
            "created_by_user_id",
            "created_at",
        ],
    ),
    (
        "order_attachments",
        OrderAttachment,
        [
            "id",
            "order_id",
            "order_item_id",
            "kind",
            "filename",
            "content_type",
            "size_bytes",
            "storage_key",
            "created_at",
        ],
    ),
]


# Archives up to this size stay in RAM; anything bigger rolls over to a
# temp file on disk, so a Pro tenant's 20 GB of drawings can't OOM-kill
# the single uvicorn worker (audit BE-16).
SPOOL_MAX_MEMORY_BYTES = 8 * 1024 * 1024
STREAM_CHUNK_BYTES = 1024 * 1024


def _copy_attachment_into_zip(zf: zipfile.ZipFile, arcname: str, storage_key: str) -> int:
    """Stream one S3 object into the archive (blocking — run in a thread)."""
    from app.storage.s3 import iter_object_chunks

    written = 0
    with zf.open(arcname, "w", force_zip64=True) as dest:
        for chunk in iter_object_chunks(storage_key, STREAM_CHUNK_BYTES):
            dest.write(chunk)
            written += len(chunk)
    return written


async def build_tenant_export_file(db: AsyncSession, *, tenant_slug: str) -> IO[bytes]:
    """Write a ZIP of the current tenant's data to a spooled temp file.

    ``db`` must be the request-scoped, RLS-bound session — that is what
    confines the export to one tenant. The returned file is rewound to
    the start; the caller streams it out and closes it.

    The DB reads stay on the event loop (the AsyncSession's connection
    belongs to it); every compression step and every S3 read runs in a
    worker thread, and attachment bodies are copied chunk by chunk, so
    neither the loop nor RAM ever holds a whole file.
    """
    out: IO[bytes] = tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_MEMORY_BYTES)  # noqa: SIM115
    errors: list[str] = []
    attachment_bytes_written = 0

    try:
        zf = zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, allowZip64=True)
        try:
            for name, model, columns in _TABLES:
                rows = list((await db.execute(select(model))).scalars().all())
                payload = _rows_to_csv(rows, columns)
                await anyio.to_thread.run_sync(zf.writestr, f"{name}.csv", payload)

            # Attachment bytes, under attachments/<order-number>/<filename>.
            attachments = list((await db.execute(select(OrderAttachment))).scalars().all())
            order_numbers = {
                o.id: o.number for o in (await db.execute(select(Order))).scalars().all()
            }
            seen: set[str] = set()
            for att in attachments:
                if attachment_bytes_written > MAX_ATTACHMENT_BYTES:
                    errors.append(
                        "Attachment export truncated at "
                        f"{MAX_ATTACHMENT_BYTES} bytes — contact support for a full archive."
                    )
                    break
                folder = order_numbers.get(att.order_id, str(att.order_id))
                arcname = f"attachments/{folder}/{att.filename}"
                # Two files with the same name on one order would collide
                # inside the archive and silently overwrite each other.
                if arcname in seen:
                    arcname = f"attachments/{folder}/{att.id}-{att.filename}"
                seen.add(arcname)
                try:
                    attachment_bytes_written += await anyio.to_thread.run_sync(
                        _copy_attachment_into_zip, zf, arcname, att.storage_key
                    )
                except Exception as exc:
                    errors.append(f"{arcname}: could not read from storage ({type(exc).__name__})")
                    log.warning(
                        "export.attachment_failed",
                        storage_key=att.storage_key,
                        error=str(exc),
                    )
                    continue

            readme = (
                f"Assoluto data export\n"
                f"Tenant: {tenant_slug}\n"
                f"Generated: {datetime.now(UTC).isoformat()}\n\n"
                "Every CSV is UTF-8 with a byte-order mark so Excel opens Czech and\n"
                "German text correctly. Identifiers are UUIDs and match across files:\n"
                "order_items.order_id refers to orders.id, and so on.\n\n"
                "attachments/ holds the uploaded files, grouped by order number.\n"
            )
            zf.writestr("README.txt", readme.encode("utf-8"))

            if errors:
                zf.writestr("_export_errors.txt", "\n".join(errors).encode("utf-8"))
        finally:
            await anyio.to_thread.run_sync(zf.close)
    except BaseException:
        out.close()
        raise

    out.seek(0)
    return out


async def build_tenant_export(db: AsyncSession, *, tenant_slug: str) -> bytes:
    """Return the export as bytes. Convenience for tests and small tenants;
    the HTTP route streams :func:`build_tenant_export_file` instead."""
    fh = await build_tenant_export_file(db, tenant_slug=tenant_slug)
    try:
        return fh.read()
    finally:
        fh.close()
