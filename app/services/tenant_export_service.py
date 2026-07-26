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
* Streams into an in-memory buffer. Fine at the scale the plans allow
  (Starter 2 GB, Pro 20 GB is the storage cap, and real tenants sit far
  below); if that stops being true this becomes a background job writing
  to S3 with an emailed link, which is why the router hands back a
  single response object rather than a file path.
* Attachment bytes are best-effort: a missing S3 object records a line
  in ``_export_errors.txt`` rather than failing the whole export. A
  partial archive is worth far more to a departing customer than a 500.
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

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


async def build_tenant_export(db: AsyncSession, *, tenant_slug: str) -> bytes:
    """Return a ZIP of the current tenant's data.

    ``db`` must be the request-scoped, RLS-bound session — that is what
    confines the export to one tenant.
    """
    buf = io.BytesIO()
    errors: list[str] = []
    attachment_bytes_written = 0

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, model, columns in _TABLES:
            rows = list((await db.execute(select(model))).scalars().all())
            zf.writestr(f"{name}.csv", _rows_to_csv(rows, columns))

        # Attachment bytes, under attachments/<order-number>/<filename>.
        attachments = list((await db.execute(select(OrderAttachment))).scalars().all())
        order_numbers = {o.id: o.number for o in (await db.execute(select(Order))).scalars().all()}
        if attachments:
            from app.storage.s3 import download_bytes

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
                    # boto3 is blocking and the app runs one uvicorn
                    # worker: downloading inline would stall every other
                    # request for the length of the export.
                    data = await run_in_threadpool(download_bytes, att.storage_key)
                except Exception as exc:
                    errors.append(f"{arcname}: could not read from storage ({type(exc).__name__})")
                    log.warning(
                        "export.attachment_failed",
                        storage_key=att.storage_key,
                        error=str(exc),
                    )
                    continue
                attachment_bytes_written += len(data)
                zf.writestr(arcname, data)

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

    return buf.getvalue()
