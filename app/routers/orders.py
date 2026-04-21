"""Orders routes: list, new, detail, items, transitions, comments."""

from __future__ import annotations

import csv
import io
from collections.abc import AsyncIterator
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import Principal, get_db, require_login
from app.i18n import t as _t
from app.models.customer import Customer
from app.models.enums import OrderStatus
from app.models.order import OrderItem
from app.security.csrf import verify_csrf
from app.services.attachment_service import list_for_order as list_attachments
from app.services.customer_service import list_customers
from app.services.notification_service import (
    build_order_status_changed,
    build_order_submitted,
)
from app.services.order_service import (
    ActorRef,
    ForbiddenActor,
    ForbiddenTransition,
    OrderAccessDenied,
    OrderError,
    OrderNotFound,
    add_comment,
    add_item,
    build_orders_query,
    create_order,
    get_order_for_principal,
    list_comments,
    list_items,
    list_orders_for_principal,
    list_status_history,
    remove_item,
    transition_order,
    update_item,
)
from app.services.product_service import search_products

router = APIRouter(prefix="/app/orders", tags=["orders"], dependencies=[Depends(verify_csrf)])


def _templates(request: Request):
    return request.app.state.templates


def _actor(principal: Principal) -> ActorRef:
    return ActorRef(
        type=principal.type,
        id=principal.id,
        customer_id=principal.customer_id,
    )


def _tenant(request: Request):
    tenant = getattr(request.state, "tenant", None)
    if tenant is None:
        raise HTTPException(status_code=500, detail="Tenant not resolved")
    return tenant


# --------------------------------------------------------------------- list


PAGE_SIZE = 20


@router.get("", response_class=HTMLResponse)
async def orders_index(
    request: Request,
    status: str | None = None,
    customer: str | None = None,
    q: str | None = None,
    page: int = 1,
    principal: Principal = Depends(require_login),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    # Parse filters defensively — invalid params get treated as "no filter".
    status_filter: OrderStatus | None = None
    if status:
        try:
            status_filter = OrderStatus(status)
        except ValueError:
            status_filter = None

    customer_filter: UUID | None = None
    if customer and principal.is_staff:
        try:
            customer_filter = UUID(customer)
        except ValueError:
            customer_filter = None

    page = max(1, page)
    offset = (page - 1) * PAGE_SIZE

    orders, total = await list_orders_for_principal(
        db,
        actor=_actor(principal),
        status_filter=status_filter,
        customer_filter=customer_filter,
        search=q,
        offset=offset,
        limit=PAGE_SIZE,
    )
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    customer_by_id: dict = {}
    customers: list = []
    if principal.is_staff:
        customers = await list_customers(db)
        customer_by_id = {c.id: c for c in customers}

    html = _templates(request).render(
        request,
        "orders/list.html",
        {
            "principal": principal,
            "tenant": _tenant(request),
            "orders": orders,
            "customer_by_id": customer_by_id,
            "customers": customers,
            "filters": {
                "status": status_filter.value if status_filter else "",
                "customer": str(customer_filter) if customer_filter else "",
                "q": q or "",
            },
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "has_active_filters": bool(
                (status or "").strip() or (customer or "").strip() or (q or "").strip()
            ),
            "status_choices": [
                ("", "All statuses"),
                ("draft", "Draft"),
                ("submitted", "Submitted"),
                ("quoted", "Quoted"),
                ("confirmed", "Confirmed"),
                ("in_production", "In production"),
                ("ready", "Ready"),
                ("delivered", "Delivered"),
                ("closed", "Closed"),
                ("cancelled", "Cancelled"),
            ],
        },
    )
    return HTMLResponse(html)


# -------------------------------------------------------------------- CSV


# Columns emitted by the CSV export, in order. Tuple of
# ``(english_header_key, Order-field extractor)``. English strings are
# the gettext message IDs; translations live in the .po catalogs.
CSV_BATCH_SIZE = 500

# UTF-8 byte-order mark — lets Excel (especially CZ locale) open the
# file with the correct encoding out of the box.
_UTF8_BOM = "﻿"


def _parse_iso_date(raw: str | None) -> date | None:
    """Parse ``YYYY-MM-DD`` tolerantly; bad/empty input yields ``None``."""
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _fmt_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    # ISO 8601 without microseconds, timezone-aware values are emitted
    # in their native offset (typically UTC — the DB stores timestamptz).
    return value.replace(microsecond=0).isoformat()


def _fmt_date(value: date | None) -> str:
    return value.isoformat() if value is not None else ""


def _fmt_decimal(value: Decimal | None) -> str:
    if value is None:
        return ""
    # Let Excel-friendly locales parse the number — emit a plain dot,
    # never scientific notation.
    return format(value, "f")


@router.get(".csv")
async def orders_export_csv(
    request: Request,
    status: str | None = None,
    customer: str | None = None,
    from_: str | None = None,
    to: str | None = None,
    q: str | None = None,
    principal: Principal = Depends(require_login),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Stream a CSV export of orders matching the same filters as the list.

    Authorization: same scoping as ``orders_index`` — staff sees the
    whole tenant (subject to RLS), contacts only their own customer's
    orders. The download filename is stamped with today's date.
    """
    # ``from`` is a Python keyword; FastAPI lets us rename via ``alias``
    # on Query(), but declaring the parameter as ``from_`` and then
    # reading ``request.query_params`` covers both "from" and "from_"
    # without adding ceremony. Keep the signature ergonomic.
    from_raw = request.query_params.get("from") or from_
    to_raw = request.query_params.get("to") or to

    status_filter: OrderStatus | None = None
    if status:
        try:
            status_filter = OrderStatus(status)
        except ValueError:
            status_filter = None

    customer_filter: UUID | None = None
    if customer and principal.is_staff:
        try:
            customer_filter = UUID(customer)
        except ValueError:
            customer_filter = None

    date_from = _parse_iso_date(from_raw)
    date_to = _parse_iso_date(to_raw)

    stmt = build_orders_query(
        actor=_actor(principal),
        status=status_filter,
        customer_id=customer_filter,
        date_from=date_from,
        date_to=date_to,
        q=q,
    )

    # Resolve customer names lazily with a per-request cache — the list
    # may contain tens of thousands of orders but typically a small
    # number of distinct customers.
    customer_names: dict[UUID, str] = {}

    async def _customer_name(cid: UUID) -> str:
        if cid in customer_names:
            return customer_names[cid]
        row = (
            await db.execute(select(Customer.name).where(Customer.id == cid))
        ).scalar_one_or_none()
        name = row or ""
        customer_names[cid] = name
        return name

    header = [
        _t(request, "Order number"),
        _t(request, "Status"),
        _t(request, "Customer"),
        _t(request, "Created at"),
        _t(request, "Submitted at"),
        _t(request, "Promised delivery at"),
        _t(request, "Quoted total"),
        _t(request, "Currency"),
        _t(request, "Items"),
    ]

    async def _row_iter() -> AsyncIterator[str]:
        buffer = io.StringIO()
        # ``;`` delimiter — CZ Excel assumes semicolons when the system
        # list separator is set that way; Excel also handles this on US
        # locales when the file is opened via the "Data > Get Data" flow.
        writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")

        # First chunk: BOM + header row.
        writer.writerow(header)
        yield _UTF8_BOM + buffer.getvalue()
        buffer.seek(0)
        buffer.truncate()

        offset = 0
        while True:
            page_stmt = stmt.offset(offset).limit(CSV_BATCH_SIZE)
            page = list((await db.execute(page_stmt)).scalars().all())
            if not page:
                break

            # Bulk-fetch item counts for this page to avoid N+1 queries.
            order_ids = [o.id for o in page]
            count_rows = await db.execute(
                select(OrderItem.order_id, func.count(OrderItem.id))
                .where(OrderItem.order_id.in_(order_ids))
                .group_by(OrderItem.order_id)
            )
            item_counts: dict[UUID, int] = {row[0]: row[1] for row in count_rows.all()}

            for order in page:
                writer.writerow(
                    [
                        order.number,
                        order.status.value,
                        await _customer_name(order.customer_id),
                        _fmt_datetime(order.created_at),
                        _fmt_datetime(order.submitted_at),
                        _fmt_date(order.promised_delivery_at),
                        _fmt_decimal(order.quoted_total),
                        order.currency,
                        str(item_counts.get(order.id, 0)),
                    ]
                )
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()

            if len(page) < CSV_BATCH_SIZE:
                break
            offset += CSV_BATCH_SIZE

    filename = f"orders-{date.today().isoformat()}.csv"
    return StreamingResponse(
        _row_iter(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ----------------------------------------------------------------- new form


@router.get("/new", response_class=HTMLResponse)
async def orders_new_form(
    request: Request,
    principal: Principal = Depends(require_login),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    customers = await list_customers(db) if principal.is_staff else []
    html = _templates(request).render(
        request,
        "orders/form.html",
        {
            "principal": principal,
            "tenant": _tenant(request),
            "customers": customers,
            "form": {},
            "today_iso": date.today().isoformat(),
            "error": None,
            "notice": None,
        },
    )
    return HTMLResponse(html)


@router.post("", response_class=HTMLResponse)
async def orders_create(
    request: Request,
    title: str = Form(...),
    customer_id: str = Form(""),
    requested_delivery_at: str = Form(""),
    notes: str = Form(""),
    principal: Principal = Depends(require_login),
    db: AsyncSession = Depends(get_db),
) -> Response:
    # Contacts always order for their own customer; staff must pick one.
    if principal.is_staff:
        try:
            selected_customer = UUID(customer_id)
        except ValueError:
            return await _rerender_form(
                request,
                db,
                principal,
                form={
                    "title": title,
                    "customer_id": customer_id,
                    "requested_delivery_at": requested_delivery_at,
                    "notes": notes,
                },
                error=_t(request, "Choose a client."),
            )
    else:
        if principal.customer_id is None:
            raise HTTPException(status_code=403, detail="Customer unknown")
        selected_customer = principal.customer_id

    try:
        parsed_date = date.fromisoformat(requested_delivery_at) if requested_delivery_at else None
    except ValueError:
        parsed_date = None

    try:
        order = await create_order(
            db,
            tenant_id=principal.tenant_id,
            actor=_actor(principal),
            customer_id=selected_customer,
            title=title,
            requested_delivery_at=parsed_date,
            notes=notes,
        )
    except (OrderError, OrderAccessDenied) as exc:
        return await _rerender_form(
            request,
            db,
            principal,
            form={
                "title": title,
                "customer_id": customer_id,
                "requested_delivery_at": requested_delivery_at,
                "notes": notes,
            },
            error=str(exc),
        )

    return RedirectResponse(url=f"/app/orders/{order.id}", status_code=303)


async def _rerender_form(
    request: Request,
    db: AsyncSession,
    principal: Principal,
    *,
    form: dict,
    error: str | None,
) -> HTMLResponse:
    customers = await list_customers(db) if principal.is_staff else []
    html = _templates(request).render(
        request,
        "orders/form.html",
        {
            "principal": principal,
            "tenant": _tenant(request),
            "customers": customers,
            "form": form,
            "today_iso": date.today().isoformat(),
            "error": error,
            "notice": None,
        },
    )
    return HTMLResponse(html, status_code=400 if error else 200)


# -------------------------------------------------------------------- detail


@router.get("/{order_id}", response_class=HTMLResponse)
async def orders_detail(
    order_id: UUID,
    request: Request,
    principal: Principal = Depends(require_login),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    try:
        order = await get_order_for_principal(db, order_id=order_id, actor=_actor(principal))
    except OrderNotFound:
        raise HTTPException(status_code=404, detail="Order not found") from None
    except OrderAccessDenied:
        raise HTTPException(status_code=404, detail="Order not found") from None

    items = await list_items(db, order.id)
    comments = await list_comments(db, order_id=order.id, include_internal=principal.is_staff)
    history = await list_status_history(db, order.id)
    attachments = await list_attachments(db, order.id)

    customer = (
        await db.execute(select(Customer).where(Customer.id == order.customer_id))
    ).scalar_one_or_none()

    available_transitions = _available_transitions(request, order, principal)

    # Resolve per-customer order permissions.
    from app.services.customer_permissions import OrderPermissions

    perms = OrderPermissions.from_dict(customer.order_permissions if customer else None)
    # Staff always gets full permissions regardless.
    if principal.is_staff:
        perms = OrderPermissions()

    # Products available on this order — only loaded when editable AND
    # the customer is allowed to use the catalog.
    product_choices: list = []
    if _can_edit_items(order, principal) and perms.can_use_catalog:
        product_choices = await search_products(
            db, query="", customer_id=order.customer_id, limit=200
        )

    html = _templates(request).render(
        request,
        "orders/detail.html",
        {
            "principal": principal,
            "tenant": _tenant(request),
            "perms": perms,
            "order": order,
            "items": items,
            "comments": comments,
            "history": history,
            "attachments": attachments,
            "customer": customer,
            "can_edit_items": _can_edit_items(order, principal),
            "available_transitions": available_transitions,
            "product_choices": product_choices,
            "error": None,
            "notice": None,
        },
    )
    return HTMLResponse(html)


def _can_edit_items(order, principal: Principal) -> bool:
    """Staff can always edit; contacts only in DRAFT."""
    if principal.is_staff:
        return True
    return order.status == OrderStatus.DRAFT


# Label + visual category for each status transition button.
# "kind" controls the button colour in the template:
#   forward  = blue (primary workflow progression)
#   back     = gray (returning to an earlier state)
#   finish   = green (completing the order)
#   danger   = red (cancel / destructive)
#
# ``label`` values are English gettext message IDs; the real translation
# is resolved per request in ``_available_transitions`` so the CS/EN
# switcher works on the action buttons too.
TRANSITION_META: dict[OrderStatus, dict] = {
    OrderStatus.DRAFT: {"label": "Return to draft", "kind": "back", "order": 0},
    OrderStatus.SUBMITTED: {"label": "Submit", "kind": "forward", "order": 1},
    OrderStatus.QUOTED: {"label": "Quote", "kind": "forward", "order": 2},
    OrderStatus.CONFIRMED: {"label": "Confirm", "kind": "forward", "order": 3},
    OrderStatus.IN_PRODUCTION: {"label": "Start production", "kind": "forward", "order": 4},
    OrderStatus.READY: {"label": "Ready", "kind": "forward", "order": 5},
    OrderStatus.DELIVERED: {"label": "Delivered", "kind": "finish", "order": 6},
    OrderStatus.CLOSED: {"label": "Close", "kind": "finish", "order": 7},
    OrderStatus.CANCELLED: {"label": "Cancel", "kind": "danger", "order": 99},
}


def _available_transitions(request: Request, order, principal: Principal) -> list[dict]:
    """Return the list of transitions the current principal can perform.

    Staff see every status except the current one — full admin control.
    Contacts see only what the state machine allows. Results are sorted
    in logical workflow order (forward first, cancel last).
    """
    from app.i18n import gettext as _gettext
    from app.services.order_service import ALL_STATUSES, CONTACT_ALLOWED_TRANSITIONS

    locale = getattr(request.state, "locale", None) or "cs"

    if principal.is_staff:
        candidates = ALL_STATUSES - {order.status}
    else:
        candidates = CONTACT_ALLOWED_TRANSITIONS.get(order.status, set())

    out: list[dict] = []
    for to_status in candidates:
        meta = TRANSITION_META.get(
            to_status, {"label": to_status.value, "kind": "forward", "order": 50}
        )
        out.append(
            {
                "to_status": to_status.value,
                "label": _gettext(locale, meta["label"]),
                "kind": meta["kind"],
                "order": meta["order"],
            }
        )
    out.sort(key=lambda x: x["order"])
    return out


# ---------------------------------------------------------------- add item


@router.post("/{order_id}/items", response_class=HTMLResponse)
async def orders_add_item(
    order_id: UUID,
    request: Request,
    description: str = Form(""),
    quantity: str = Form(...),
    unit: str = Form("ks"),
    unit_price: str = Form(""),
    product_id: str = Form(""),
    principal: Principal = Depends(require_login),
    db: AsyncSession = Depends(get_db),
) -> Response:
    try:
        order = await get_order_for_principal(db, order_id=order_id, actor=_actor(principal))
    except (OrderNotFound, OrderAccessDenied):
        raise HTTPException(status_code=404, detail="Order not found") from None

    # Server-side enforcement of per-customer OrderPermissions — the
    # template hides the form but a direct POST bypasses the UI.
    # Round-4 audit A1 fix.
    if not principal.is_staff:
        from app.models.customer import Customer
        from app.services.customer_permissions import OrderPermissions

        customer = (
            await db.execute(select(Customer).where(Customer.id == order.customer_id))
        ).scalar_one_or_none()
        perms = OrderPermissions.from_dict(customer.order_permissions if customer else None)
        if not perms.can_add_items:
            raise HTTPException(status_code=403, detail="Adding items is disabled for your account")
        if unit_price.strip() and not perms.can_set_prices:
            raise HTTPException(
                status_code=403, detail="Setting prices is disabled for your account"
            )

    try:
        qty = Decimal(quantity)
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=400, detail="Invalid quantity") from None

    price: Decimal | None = None
    if unit_price.strip():
        try:
            price = Decimal(unit_price)
        except (InvalidOperation, ValueError):
            raise HTTPException(status_code=400, detail="Invalid unit_price") from None

    # Optional product link — when provided, look it up and back-fill the
    # line item's description/unit/price from the catalog unless the caller
    # overrode them in the form.
    product_uuid: UUID | None = None
    if product_id.strip():
        try:
            product_uuid = UUID(product_id.strip())
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid product_id") from None

        from app.models.product import Product

        product = (
            await db.execute(select(Product).where(Product.id == product_uuid))
        ).scalar_one_or_none()
        if product is None:
            raise HTTPException(status_code=400, detail="Unknown product") from None

        if not description.strip():
            description = f"{product.sku} — {product.name}"
        if not unit or unit == "ks":
            unit = product.unit
        if price is None and product.default_price is not None:
            price = product.default_price

    if not description.strip():
        raise HTTPException(status_code=400, detail="description required") from None

    try:
        await add_item(
            db,
            tenant_id=principal.tenant_id,
            order=order,
            actor=_actor(principal),
            description=description,
            quantity=qty,
            unit=unit or "ks",
            unit_price=price,
            product_id=product_uuid,
        )
    except ForbiddenTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except OrderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    return RedirectResponse(url=f"/app/orders/{order.id}", status_code=303)


@router.api_route(
    "/{order_id}/items/{item_id}/patch",
    methods=["POST", "PATCH"],
    response_class=HTMLResponse,
)
async def orders_patch_item(
    order_id: UUID,
    item_id: UUID,
    request: Request,
    quantity: str = Form(""),
    unit_price: str = Form(""),
    note: str = Form(""),
    principal: Principal = Depends(require_login),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Field-level autosave for a single order-item row.

    Called by HTMX from ``_item_row.html`` whenever one of the inline
    inputs changes (quantity, price, note). Accepts any subset — fields
    left blank are skipped so typing in the note box does not clobber
    the quantity on the next keystroke.

    Always returns an HTML fragment (the updated row). Validation errors
    come back as a 200-with-error-fragment so HTMX swaps in place and the
    user sees the red hint next to the offending input; a genuine state
    error (order not DRAFT) is a 409 with the same fragment structure.
    """
    try:
        order = await get_order_for_principal(db, order_id=order_id, actor=_actor(principal))
    except (OrderNotFound, OrderAccessDenied):
        raise HTTPException(status_code=404, detail="Order not found") from None

    from app.models.order import OrderItem

    item = (
        await db.execute(
            select(OrderItem).where(OrderItem.id == item_id, OrderItem.order_id == order_id)
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")

    # Per-customer permission guard — same shape as add_item. Restricts
    # contacts from setting prices if the customer config forbids it.
    if not principal.is_staff:
        customer = (
            await db.execute(select(Customer).where(Customer.id == order.customer_id))
        ).scalar_one_or_none()
        from app.services.customer_permissions import OrderPermissions

        perms = OrderPermissions.from_dict(customer.order_permissions if customer else None)
        can_set_prices = perms.can_set_prices
    else:
        from app.services.customer_permissions import OrderPermissions

        perms = OrderPermissions()
        can_set_prices = True

    qty: Decimal | None = None
    price: Decimal | None = None
    note_value: str | None = None

    row_error: str | None = None

    if quantity.strip():
        try:
            qty = Decimal(quantity)
        except (InvalidOperation, ValueError):
            row_error = _t(request, "Invalid quantity")

    if row_error is None and unit_price.strip():
        if not can_set_prices:
            raise HTTPException(
                status_code=403, detail="Setting prices is disabled for your account"
            )
        try:
            price = Decimal(unit_price)
        except (InvalidOperation, ValueError):
            row_error = _t(request, "Invalid unit_price")

    # The note field is a free-form string; empty-string means "clear".
    # The caller sends it on every change because the whole row is
    # serialised (hx-include="closest tr"); we distinguish "blank was
    # typed" from "field was absent" by always applying it when the form
    # field exists at all.
    if "note" in (await request.form()):
        note_value = note

    status_code = 200
    if row_error is None:
        try:
            await update_item(
                db,
                order=order,
                item=item,
                quantity=qty,
                unit_price=price,
                note=note_value,
                actor=_actor(principal),
            )
            await db.commit()
        except ForbiddenTransition as exc:
            # Order moved out of DRAFT between the page load and the
            # autosave — return the row with an inline error so the user
            # sees why the change didn't stick.
            row_error = _t(request, "This order is no longer a draft.")
            status_code = 409
            await db.rollback()
            _ = exc
        except OrderAccessDenied:
            raise HTTPException(status_code=404, detail="Order not found") from None
        except OrderError as exc:
            row_error = str(exc)
            await db.rollback()

    # Re-read the item so the rendered row reflects whatever actually
    # landed in the DB (including ``line_total`` recompute on success).
    await db.refresh(item)

    # Find the 1-based index among the order's items for the "#" column
    # display — cheap; at most a few dozen rows per order.
    siblings = await list_items(db, order.id)
    try:
        row_index = [s.id for s in siblings].index(item.id) + 1
    except ValueError:
        row_index = 0

    html = _templates(request).render(
        request,
        "orders/_item_row.html",
        {
            "order": order,
            "item": item,
            "row_index": row_index,
            "can_edit_items": _can_edit_items(order, principal),
            "can_set_prices": can_set_prices,
            "is_staff": principal.is_staff,
            "row_error": row_error,
            "saved": row_error is None,
            "perms": perms,
        },
    )
    return HTMLResponse(html, status_code=status_code)


@router.post("/{order_id}/items/{item_id}/delete", response_class=HTMLResponse)
async def orders_delete_item(
    order_id: UUID,
    item_id: UUID,
    request: Request,
    principal: Principal = Depends(require_login),
    db: AsyncSession = Depends(get_db),
) -> Response:
    try:
        order = await get_order_for_principal(db, order_id=order_id, actor=_actor(principal))
    except (OrderNotFound, OrderAccessDenied):
        raise HTTPException(status_code=404, detail="Order not found") from None

    from app.models.order import OrderItem

    item = (
        await db.execute(
            select(OrderItem).where(OrderItem.id == item_id, OrderItem.order_id == order_id)
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")

    try:
        await remove_item(db, order=order, item=item, actor=_actor(principal))
    except ForbiddenTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None

    return RedirectResponse(url=f"/app/orders/{order.id}", status_code=303)


# --------------------------------------------------------------- transition


@router.post("/{order_id}/transitions/{to_status}", response_class=HTMLResponse)
async def orders_transition(
    order_id: UUID,
    to_status: str,
    request: Request,
    background_tasks: BackgroundTasks,
    principal: Principal = Depends(require_login),
    db: AsyncSession = Depends(get_db),
) -> Response:
    try:
        target = OrderStatus(to_status)
    except ValueError:
        raise HTTPException(status_code=400, detail="Unknown status") from None

    try:
        order = await get_order_for_principal(db, order_id=order_id, actor=_actor(principal))
    except (OrderNotFound, OrderAccessDenied):
        raise HTTPException(status_code=404, detail="Order not found") from None

    try:
        await transition_order(
            db,
            order=order,
            to_status=target,
            actor=_actor(principal),
        )
    except (ForbiddenTransition, ForbiddenActor) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except OrderAccessDenied:
        raise HTTPException(status_code=404, detail="Order not found") from None

    # Build notifications BEFORE we commit + schedule the background task:
    # queries still run under RLS inside the current session.
    tenant = request.state.tenant
    settings = request.app.state.settings
    sender = request.app.state.email_sender
    from app.urls import tenant_base_url

    tenant_url = tenant_base_url(settings, tenant)

    notif_submitted = None
    notif_status = None
    if target == OrderStatus.SUBMITTED:
        notif_submitted = await build_order_submitted(
            db,
            tenant_name=tenant.name,
            order=order,
            base_url=tenant_url,
        )
    else:
        notif_status = await build_order_status_changed(
            db,
            tenant_name=tenant.name,
            order=order,
            to_status=target,
            base_url=tenant_url,
        )

    # Commit so the background task's fresh session can see the new state.
    await db.commit()

    if notif_submitted is not None:
        from app.tasks.email_tasks import send_order_submitted

        background_tasks.add_task(
            send_order_submitted,
            sender,
            recipients=notif_submitted.recipients,
            tenant_name=notif_submitted.tenant_name,
            customer_name=notif_submitted.customer_name,
            order_number=notif_submitted.order_number,
            order_title=notif_submitted.order_title,
            order_url=notif_submitted.order_url,
        )
    if notif_status is not None:
        from app.tasks.email_tasks import send_order_status_changed

        background_tasks.add_task(
            send_order_status_changed,
            sender,
            recipients=notif_status.recipients,
            tenant_name=notif_status.tenant_name,
            order_number=notif_status.order_number,
            order_title=notif_status.order_title,
            order_url=notif_status.order_url,
            to_status=notif_status.to_status,
        )

    return RedirectResponse(url=f"/app/orders/{order.id}", status_code=303)


# ------------------------------------------------------------------ comments


@router.post("/{order_id}/comments", response_class=HTMLResponse)
async def orders_add_comment(
    order_id: UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    body: str = Form(...),
    is_internal: str = Form(""),
    principal: Principal = Depends(require_login),
    db: AsyncSession = Depends(get_db),
) -> Response:
    try:
        order = await get_order_for_principal(db, order_id=order_id, actor=_actor(principal))
    except (OrderNotFound, OrderAccessDenied):
        raise HTTPException(status_code=404, detail="Order not found") from None

    internal_flag = bool(is_internal)
    try:
        await add_comment(
            db,
            tenant_id=principal.tenant_id,
            order=order,
            actor=_actor(principal),
            body=body,
            is_internal=internal_flag,
        )
    except ForbiddenActor as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except OrderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    # Build comment notification while the session is still open; skip
    # internal comments entirely (those are staff-only).
    notif = None
    if not internal_flag:
        from app.services.notification_service import build_order_comment

        tenant = request.state.tenant
        settings = request.app.state.settings
        from app.urls import tenant_base_url

        notif = await build_order_comment(
            db,
            tenant_name=tenant.name,
            order=order,
            author_email=principal.email,
            author_name=principal.full_name,
            author_is_staff=principal.is_staff,
            body=body,
            base_url=tenant_base_url(settings, tenant),
        )

    await db.commit()

    if notif is not None:
        from app.tasks.email_tasks import send_order_comment

        sender = request.app.state.email_sender
        background_tasks.add_task(
            send_order_comment,
            sender,
            recipients=notif.recipients,
            tenant_name=notif.tenant_name,
            order_number=notif.order_number,
            order_title=notif.order_title,
            order_url=notif.order_url,
            author_name=notif.author_name,
            body_excerpt=notif.body_excerpt,
        )

    return RedirectResponse(url=f"/app/orders/{order.id}", status_code=303)
