"""Orders routes: list, new, detail, items, transitions, comments."""

from __future__ import annotations

import csv
import io
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from decimal import Decimal
from urllib.parse import quote, urlencode
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import Principal, get_db, require_login, require_tenant_staff
from app.i18n import t as _t
from app.models.customer import Customer
from app.models.enums import STATUS_LABELS, OrderStatus
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.routers._amounts import amount_error_message
from app.security.csrf import verify_csrf
from app.services.attachment_service import list_for_order as list_attachments
from app.services.audit_service import actor_from_principal
from app.services.customer_service import list_customers
from app.services.money import AmountError, parse_money, parse_quantity
from app.services.notification_service import (
    build_order_created,
    build_order_status_changed,
    build_order_submitted,
    merge_for_digest,
)
from app.services.order_service import (
    DEFAULT_STALE_QUOTE_DAYS,
    NO_TOTAL_CHECK,
    UNASSIGNED,
    WORK_QUEUES,
    ActorRef,
    EmptyOrder,
    ForbiddenActor,
    ForbiddenTransition,
    IncompleteQuote,
    InvalidAmount,
    OrderAccessDenied,
    OrderError,
    OrderNotFound,
    QuoteChanged,
    add_comment,
    add_item,
    assign_order,
    build_orders_query,
    bulk_transition,
    create_order,
    duplicate_order,
    get_order_for_principal,
    last_prices_for_customer,
    list_assignable_staff,
    list_comments,
    list_items,
    list_orders_for_principal,
    list_status_history,
    quote_problems,
    remove_item,
    transition_order,
    update_item,
    update_order_header,
)
from app.services.product_service import catalog_for_customer
from app.timezones import local_today, request_tz, to_local, tz_label

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


def _parse_assigned_filter(raw: str | None, principal: Principal) -> UUID | str | None:
    """Parse the ``assigned`` query param into a ``build_orders_query`` value.

    Shared by the list and the CSV export: the export link forwards the
    whole query string, so parsing it in only one of the two silently
    hands the user more rows than the screen showed them.

    ``me`` resolves server-side so a bookmarked or shared filter shows
    each colleague their own queue. Staff-only, like the customer filter
    — a contact has no business slicing the supplier's workload.
    """
    if not raw or not principal.is_staff:
        return None
    if raw == "me":
        return principal.id
    if raw == UNASSIGNED:
        return UNASSIGNED
    try:
        return UUID(raw)
    except ValueError:
        return None


def _stale_quote_days(request: Request) -> int:
    """Days after which an unanswered quote needs chasing (setting)."""
    settings = getattr(request.app.state, "settings", None)
    return int(getattr(settings, "quote_reminder_days", 0) or DEFAULT_STALE_QUOTE_DAYS)


def _queue_labels(request: Request) -> dict[str, str]:
    """Human names of the "needs action" queues (IDEA-1)."""
    return {
        "awaiting_quote": _t(request, "Submitted, waiting for a quote"),
        "no_promise": _t(request, "Confirmed without a promised date"),
        "overdue": _t(request, "Overdue"),
        "stale_quotes": _t(request, "Quotes waiting for the client"),
    }


def _query_without(request: Request, *drop: str) -> str:
    """The current query string minus ``drop`` (and one-shot flashes)."""
    skip = {*drop, "notice", "error"}
    return urlencode([(k, v) for k, v in request.query_params.multi_items() if k not in skip])


@router.get("", response_class=HTMLResponse)
async def orders_index(
    request: Request,
    status: str | None = None,
    customer: str | None = None,
    assigned: str | None = None,
    q: str | None = None,
    queue: str | None = None,
    sort: str | None = None,
    page: int = 1,
    notice: str | None = None,
    error: str | None = None,
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

    assigned_filter = _parse_assigned_filter(assigned, principal)
    queue_filter = queue if (principal.is_staff and queue in WORK_QUEUES) else None
    sort_key = sort if sort in ("due", "-due") else None

    page = max(1, page)
    offset = (page - 1) * PAGE_SIZE

    orders, total = await list_orders_for_principal(
        db,
        actor=_actor(principal),
        status_filter=status_filter,
        customer_filter=customer_filter,
        search=q,
        assigned_filter=assigned_filter,
        queue=queue_filter,
        sort=sort_key,
        stale_quote_days=_stale_quote_days(request),
        offset=offset,
        limit=PAGE_SIZE,
        tz=request_tz(request),
    )
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    customer_by_id: dict = {}
    customers: list = []
    staff_choices: list = []
    assignee_by_id: dict = {}
    if principal.is_staff:
        customers = await list_customers(db)
        customer_by_id = {c.id: c for c in customers}
        staff_choices = await list_assignable_staff(db)
        # Names for the "Owner" column. Built from the picker list, which
        # is already loaded; an owner who has since been deactivated falls
        # back to a plain lookup so the column never goes blank on a row
        # that genuinely has an owner.
        assignee_by_id = {u.id: u.full_name for u in staff_choices}
        missing = {
            o.assigned_to_user_id
            for o in orders
            if o.assigned_to_user_id is not None and o.assigned_to_user_id not in assignee_by_id
        }
        if missing:
            from app.models.user import User as _User

            rows = (
                await db.execute(select(_User.id, _User.full_name).where(_User.id.in_(missing)))
            ).all()
            assignee_by_id.update({row[0]: row[1] for row in rows})

    # Status options for the bulk-transition dropdown (staff only in the
    # template; listed without the empty option). English labels stay in
    # sync with the filter list above so a single translation catalog
    # covers both.
    bulk_status_choices = [
        ("draft", "Draft"),
        ("submitted", "Submitted"),
        ("quoted", "Quoted"),
        ("confirmed", "Confirmed"),
        ("in_production", "In production"),
        ("ready", "Ready"),
        ("delivered", "Delivered"),
        ("closed", "Closed"),
        ("cancelled", "Cancelled"),
    ]

    html = _templates(request).render(
        request,
        "orders/list.html",
        {
            "principal": principal,
            "tenant": _tenant(request),
            "orders": orders,
            "customer_by_id": customer_by_id,
            "customers": customers,
            "staff_choices": staff_choices,
            "assignee_by_id": assignee_by_id,
            "filters": {
                "status": status_filter.value if status_filter else "",
                "customer": str(customer_filter) if customer_filter else "",
                "assigned": (assigned or "") if principal.is_staff else "",
                "q": q or "",
                "queue": queue_filter or "",
                "sort": sort_key or "",
            },
            # Query string for pagination and the sort toggle: every
            # current filter except the page itself. The pager used to
            # rebuild it by hand and dropped ``assigned`` (UX-08), so
            # page 2 of "Assigned to me" showed everyone's orders.
            "page_qs": _query_without(request, "page"),
            "sort_qs": _query_without(request, "page", "sort"),
            "queue_labels": _queue_labels(request),
            "today": local_today(request_tz(request)),
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "has_active_filters": bool(
                (status or "").strip()
                or (customer or "").strip()
                or (assigned or "").strip()
                or (q or "").strip()
                or queue_filter
            ),
            "notice": notice or None,
            "error": error or None,
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
            "bulk_status_choices": bulk_status_choices,
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


@dataclass(frozen=True)
class CsvDialect:
    """How a spreadsheet in the user's language expects a CSV (P3-11).

    Czech and German Excel use ``;`` as the list separator and a decimal
    comma, and read ``dd.mm.yyyy`` as a date; English Excel wants ``,``
    and a dot. The file is UTF-8 with a BOM either way.
    """

    delimiter: str
    decimal_comma: bool
    day_first: bool


_CSV_DIALECTS: dict[str, CsvDialect] = {
    "cs": CsvDialect(delimiter=";", decimal_comma=True, day_first=True),
    "de": CsvDialect(delimiter=";", decimal_comma=True, day_first=True),
}
_CSV_DEFAULT_DIALECT = CsvDialect(delimiter=",", decimal_comma=False, day_first=False)


def csv_dialect(locale: str | None) -> CsvDialect:
    """The CSV dialect for a UI locale (anything unknown: English)."""
    return _CSV_DIALECTS.get((locale or "").split("-", 1)[0].lower(), _CSV_DEFAULT_DIALECT)


def _fmt_datetime(
    value: datetime | None, tz: tzinfo | None = None, dialect: CsvDialect = _CSV_DEFAULT_DIALECT
) -> str:
    """A UTC instant as wall-clock time in the tenant's zone ``tz``.

    Day-first locales get ``03.03.2026 00:30`` (what Czech / German
    Excel recognises as a date-time); otherwise ISO 8601 with the local
    offset (``2026-03-03T00:30:00+01:00``) so a machine can still recover
    the instant. The header names the zone either way (E2 / LOGIC-16).
    """
    if value is None:
        return ""
    local = to_local(value, tz).replace(microsecond=0)
    if dialect.day_first:
        return local.strftime("%d.%m.%Y %H:%M")
    return local.isoformat()


def _fmt_date(value: date | None, dialect: CsvDialect = _CSV_DEFAULT_DIALECT) -> str:
    if value is None:
        return ""
    return value.strftime("%d.%m.%Y") if dialect.day_first else value.isoformat()


def _fmt_decimal(value: Decimal | None, dialect: CsvDialect = _CSV_DEFAULT_DIALECT) -> str:
    if value is None:
        return ""
    # Plain digits, never scientific notation and no thousands grouping
    # (a grouped number is text to Excel); the decimal mark matches the
    # spreadsheet's locale so the column sums.
    text = format(value, "f")
    return text.replace(".", ",") if dialect.decimal_comma else text


@router.get(".csv")
async def orders_export_csv(
    request: Request,
    status: str | None = None,
    customer: str | None = None,
    assigned: str | None = None,
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
    # ``from`` / ``to`` are calendar days in the tenant's zone, and the
    # timestamp columns are written in that zone too.
    tz = request_tz(request)

    stmt = build_orders_query(
        actor=_actor(principal),
        status=status_filter,
        customer_id=customer_filter,
        assigned_to=_parse_assigned_filter(assigned, principal),
        date_from=date_from,
        date_to=date_to,
        q=q,
        tz=tz,
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

    zone = tz_label(tz)
    dialect = csv_dialect(getattr(request.state, "locale", None))
    # Status column in words, in the user's language (not "in_production").
    status_names = {s: _t(request, label) for s, label in STATUS_LABELS.items()}
    header = [
        _t(request, "Order number"),
        _t(request, "Status"),
        _t(request, "Customer"),
        f"{_t(request, 'Created at')} ({zone})",
        f"{_t(request, 'Submitted at')} ({zone})",
        _t(request, "Promised delivery at"),
        _t(request, "Quoted total"),
        _t(request, "Currency"),
        _t(request, "Items"),
    ]

    async def _row_iter() -> AsyncIterator[str]:
        buffer = io.StringIO()
        # ``;`` for cs / de (their Excel's list separator), ``,`` otherwise.
        writer = csv.writer(buffer, delimiter=dialect.delimiter, lineterminator="\r\n")

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
                        status_names.get(order.status, order.status.value),
                        await _customer_name(order.customer_id),
                        _fmt_datetime(order.created_at, tz, dialect),
                        _fmt_datetime(order.submitted_at, tz, dialect),
                        _fmt_date(order.promised_delivery_at, dialect),
                        _fmt_decimal(order.quoted_total, dialect),
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

    filename = f"orders-{local_today(tz).isoformat()}.csv"
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
            "today_iso": local_today(request_tz(request)).isoformat(),
            "error": None,
            "notice": None,
        },
    )
    return HTMLResponse(html)


@router.post("", response_class=HTMLResponse)
async def orders_create(
    request: Request,
    background_tasks: BackgroundTasks,
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

    # Staff opening an order for a customer: tell the customer. Without
    # this they heard nothing until the first status transition, which for
    # a job agreed over the phone could be days later.
    notifications: list = []
    if principal.is_staff:
        tenant = request.state.tenant
        settings = request.app.state.settings
        from app.urls import tenant_base_url

        notifications = await build_order_created(
            db,
            tenant=tenant,
            order=order,
            base_url=tenant_base_url(settings, tenant),
            settings=settings,
            author_name=principal.full_name,
            actor_email=principal.email,
        )

    # Commit before scheduling — the task opens a fresh session and must
    # see the new row (CLAUDE.md §2).
    await db.commit()

    if notifications:
        from app.tasks.email_tasks import send_order_notifications

        background_tasks.add_task(
            send_order_notifications, request.app.state.email_sender, notifications
        )

    notice = quote(_t(request, "Order created."))
    return RedirectResponse(url=f"/app/orders/{order.id}?notice={notice}", status_code=303)


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
            "today_iso": local_today(request_tz(request)).isoformat(),
            "error": error,
            "notice": None,
        },
    )
    return HTMLResponse(html, status_code=400 if error else 200)


# ----------------------------------------------------------------- PDF export


@router.get("/{order_id}.pdf")
async def orders_pdf(
    order_id: UUID,
    request: Request,
    principal: Principal = Depends(require_login),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Render the order as a PDF for inline browser viewing / saving.

    Authorisation mirrors the HTML detail view: staff always, contacts only
    for their own customer's orders. Access denial returns 404 so we don't
    leak the existence of an order on another customer.
    """
    try:
        order = await get_order_for_principal(db, order_id=order_id, actor=_actor(principal))
    except OrderNotFound:
        raise HTTPException(status_code=404, detail="Order not found") from None
    except OrderAccessDenied:
        raise HTTPException(status_code=404, detail="Order not found") from None

    items = await list_items(db, order.id)
    customer = (
        await db.execute(select(Customer).where(Customer.id == order.customer_id))
    ).scalar_one_or_none()
    tenant = _tenant(request)
    locale = getattr(request.state, "locale", None) or "cs"

    # SKU column: the linked catalog product's code (demo review P2-3).
    product_ids = {i.product_id for i in items if i.product_id is not None}
    skus: dict[UUID, str] = {}
    if product_ids:
        rows = await db.execute(select(Product.id, Product.sku).where(Product.id.in_(product_ids)))
        skus = dict(rows.tuples().all())

    # Local import so reportlab is only loaded when PDFs are actually served.
    from app.services.pdf_service import render_order_pdf

    pdf_bytes = render_order_pdf(order, items, customer, tenant, locale=locale, skus=skus)

    from io import BytesIO

    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{order.number}.pdf"',
            "Content-Length": str(len(pdf_bytes)),
        },
    )


# -------------------------------------------------------------------- detail


@router.get("/{order_id}", response_class=HTMLResponse)
async def orders_detail(
    order_id: UUID,
    request: Request,
    notice: str | None = None,
    error: str | None = None,
    product_q: str = "",
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

    # Resolve comment author labels in one batch so the template can
    # render "<author> · 2026-05-09" instead of an anonymous timestamp.
    comment_authors: dict[UUID, str] = {}
    user_ids = {c.author_user_id for c in comments if c.author_user_id is not None}
    contact_ids = {c.author_contact_id for c in comments if c.author_contact_id is not None}
    if user_ids:
        from app.models.user import User as _User

        rows = (
            await db.execute(select(_User.id, _User.full_name).where(_User.id.in_(user_ids)))
        ).all()
        for row in rows:
            comment_authors[row[0]] = row[1]
    if contact_ids:
        from app.models.customer import CustomerContact as _Contact

        rows = (
            await db.execute(
                select(_Contact.id, _Contact.full_name).where(_Contact.id.in_(contact_ids))
            )
        ).all()
        for row in rows:
            comment_authors[row[0]] = row[1]

    customer = (
        await db.execute(select(Customer).where(Customer.id == order.customer_id))
    ).scalar_one_or_none()

    problems = await quote_problems(db, order)
    status_pipeline = _status_pipeline(request, order, principal, quote_problems=problems)

    # Assignment picker — staff only. Contacts must not see the
    # supplier's internal staff list, let alone who is working on what.
    staff_choices: list = []
    assignee_name = ""
    assignee_is_pickable = True
    if principal.is_staff:
        staff_choices = await list_assignable_staff(db)
        if order.assigned_to_user_id is not None:
            assignee_name = next(
                (u.full_name for u in staff_choices if u.id == order.assigned_to_user_id),
                "",
            )
            if not assignee_name:
                # Owner has since been deactivated, so they are not in the
                # picker. Look them up anyway: rendering the select with no
                # option selected makes the browser show the first one
                # ("Unassigned"), and re-saving the form would then clear a
                # real assignment nobody chose to clear.
                from app.models.user import User as _User

                assignee_row = (
                    await db.execute(
                        select(_User.full_name).where(_User.id == order.assigned_to_user_id)
                    )
                ).first()
                assignee_name = assignee_row[0] if assignee_row else ""
                assignee_is_pickable = False

    # Resolve per-customer order permissions.
    from app.services.customer_permissions import OrderPermissions

    perms = OrderPermissions.from_dict(customer.order_permissions if customer else None)
    # Staff always gets full permissions regardless.
    if principal.is_staff:
        perms = OrderPermissions()

    # Products available on this order — only loaded when editable AND
    # the customer is allowed to use the catalog.
    product_choices: list = []
    product_total = 0
    last_prices: dict = {}
    if _can_edit_items(order, principal) and perms.can_use_catalog:
        # Server-side filter instead of a silent 200-row dump (BE-15):
        # the picker shows "n of N" and a search box whenever the
        # catalog is larger than one page. A customer's own SKU replaces
        # the shared one for the same SKU (LOGIC-14).
        product_choices, product_total = await catalog_for_customer(
            db, customer_id=order.customer_id, query=product_q, limit=PRODUCT_PICKER_LIMIT
        )
        if principal.is_staff and product_choices:
            # Price memory (IDEA-6): what this customer last paid for each
            # product. Staff only — it is the supplier's own pricing.
            last_prices = await last_prices_for_customer(
                db,
                customer_id=order.customer_id,
                product_ids=[p.id for p in product_choices],
                exclude_order_id=order.id,
            )

    from app.services.order_service import is_overdue, pipeline_rank

    current_rank = pipeline_rank(order.status)
    confirmed_rank = pipeline_rank(OrderStatus.CONFIRMED)

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
            "comment_authors": comment_authors,
            "history": history,
            "attachments": attachments,
            "customer": customer,
            "can_edit_items": _can_edit_items(order, principal),
            "status_pipeline": status_pipeline,
            "staff_choices": staff_choices,
            "assignee_name": assignee_name,
            "assignee_is_pickable": assignee_is_pickable,
            "product_choices": product_choices,
            "product_total": product_total,
            "product_q": product_q,
            "product_limit": PRODUCT_PICKER_LIMIT,
            "last_prices": last_prices,
            "is_overdue": is_overdue(order, today=local_today(request_tz(request))),
            # Past the agreement — a deleted drawing there is evidence lost.
            "is_agreed": current_rank is not None and current_rank >= (confirmed_rank or 0),
            "can_reorder": principal.is_staff or perms.can_add_items,
            "error": error,
            "notice": notice,
        },
    )
    return HTMLResponse(html)


#: Rows rendered in the order-item product picker before the user has to
#: narrow it down with the search box.
PRODUCT_PICKER_LIMIT = 200


def _can_edit_items(order, principal: Principal) -> bool:
    """Staff edit in DRAFT/SUBMITTED/QUOTED (mirrors STAFF_ITEM_EDIT_STATES
    in order_service); contacts only in DRAFT.
    """
    from app.services.order_service import STAFF_ITEM_EDIT_STATES

    if principal.is_staff:
        return order.status in STAFF_ITEM_EDIT_STATES
    return order.status == OrderStatus.DRAFT


# Status *nouns* — what the order currently is — now live next to the
# enum in ``app.models.enums`` so the stepper, the emails and the order
# PDF cannot drift apart. These msgids are shared with
# ``orders/_status_badge.html`` and the filter dropdowns, so the
# translations already exist in the CS/DE catalogs.

# Status *verbs* — what clicking does. Used only for the single primary
# call-to-action button; every other node in the stepper is labelled
# with its noun, because "jump the order to Ready" reads better as a
# position on the pipeline than as a command.
STATUS_ACTIONS: dict[OrderStatus, str] = {
    OrderStatus.DRAFT: "Return to draft",
    OrderStatus.SUBMITTED: "Submit",
    OrderStatus.QUOTED: "Quote",
    OrderStatus.CONFIRMED: "Confirm",
    OrderStatus.IN_PRODUCTION: "Start production",
    OrderStatus.READY: "Mark as ready",
    OrderStatus.DELIVERED: "Mark as delivered",
    OrderStatus.CLOSED: "Close order",
    OrderStatus.CANCELLED: "Cancel order",
}


def _status_pipeline(
    request: Request,
    order,
    principal: Principal,
    *,
    quote_problems: list[str] | None = None,
) -> dict:
    """Build the view-model for the order status stepper.

    The stepper renders the whole pipeline at once — past steps ticked,
    the current one highlighted, future ones reachable. That replaces the
    old flat row of buttons, whose fatal flaw was labelling moves by
    *target status only*: from CONFIRMED, a backward move to QUOTED came
    out as a blue button reading "Quote", visually identical to a step
    forward. Position on a line is unambiguous in a way a verb is not.

    Staff can click any node (see ``STAFF_ALLOWED_TRANSITIONS``);
    contacts get the same picture of where their order stands, but only
    the nodes their own graph permits are clickable. Jumps that skip
    steps or move backwards carry a ``confirm`` message — the freedom is
    the point, but it should never be *accidental*.

    ``quote_problems`` (from :func:`order_service.quote_problems`) marks
    the QUOTED / CONFIRMED nodes: staff get a "send anyway?" confirm and
    an ``allow_incomplete`` flag (LOGIC-7); contacts cannot confirm an
    unpriced quote at all, so the node is rendered as a plain marker.
    """
    from app.services.order_service import (
        CONTACT_ALLOWED_TRANSITIONS,
        PIPELINE,
        STAFF_ALLOWED_TRANSITIONS,
        pipeline_rank,
        skipped_statuses,
    )

    problems = list(quote_problems or [])
    if principal.is_staff:
        allowed = set(STAFF_ALLOWED_TRANSITIONS.get(order.status, set()))
    else:
        allowed = set(CONTACT_ALLOWED_TRANSITIONS.get(order.status, set()))
        if problems:
            allowed.discard(OrderStatus.CONFIRMED)

    current = order.status
    current_rank = pipeline_rank(current)
    is_cancelled = current == OrderStatus.CANCELLED

    def _label(status: OrderStatus) -> str:
        return _t(request, STATUS_LABELS.get(status, status.value))

    def _incomplete_warning() -> str:
        if "no_items" in problems:
            return _t(request, "This order has no items yet. Continue anyway?")
        return _t(request, "Some items have no price yet. Continue anyway?")

    def _confirm_for(target: OrderStatus) -> str | None:
        """Guard message for a move that isn't the obvious next step."""
        if target == OrderStatus.CANCELLED:
            return _t(request, "Cancel this order?")
        messages: list[str] = []
        if problems and target in (OrderStatus.QUOTED, OrderStatus.CONFIRMED):
            messages.append(_incomplete_warning())
        skipped = skipped_statuses(current, target)
        target_rank = pipeline_rank(target)
        if skipped:
            messages.append(
                _t(
                    request, "This jumps straight to {target} and skips {skipped}. Continue?"
                ).format(
                    target=_label(target),
                    skipped=", ".join(_label(s) for s in skipped),
                )
            )
        elif current_rank is not None and target_rank is not None and target_rank < current_rank:
            messages.append(
                _t(request, "This moves the order back to {target}. Continue?").format(
                    target=_label(target)
                )
            )
        return " ".join(messages) or None

    def _node(status: OrderStatus) -> dict:
        rank = pipeline_rank(status)
        if is_cancelled or current_rank is None or rank is None:
            state = "current" if status == current else "future"
        elif rank < current_rank:
            state = "done"
        elif rank == current_rank:
            state = "current"
        else:
            state = "future"
        return {
            "value": status.value,
            "label": _label(status),
            "action_label": _t(request, STATUS_ACTIONS.get(status, status.value)),
            "state": state,
            "allowed": status in allowed,
            "confirm": _confirm_for(status) if status in allowed else None,
            # Staff override of the LOGIC-7 guard, sent only after the
            # confirm above has been accepted.
            "allow_incomplete": bool(
                principal.is_staff
                and problems
                and status in (OrderStatus.QUOTED, OrderStatus.CONFIRMED)
            ),
            # The confirm form posts the total it showed (LOGIC-2).
            "expects_total": status == OrderStatus.CONFIRMED,
            # Quoting / confirming is where a delivery date is promised
            # (IDEA-4); the primary CTA offers the date input.
            "asks_promise": principal.is_staff
            and status in (OrderStatus.QUOTED, OrderStatus.CONFIRMED),
        }

    nodes = [_node(s) for s in PIPELINE]

    # The natural next step gets a prominent CTA so the common case stays
    # one obvious click — the stepper adds freedom without making the
    # happy path harder to find.
    primary = None
    if current_rank is not None and current_rank + 1 < len(PIPELINE):
        candidate = PIPELINE[current_rank + 1]
        if candidate in allowed:
            primary = _node(candidate)

    cancel = _node(OrderStatus.CANCELLED) if OrderStatus.CANCELLED in allowed else None
    # Reopening a cancelled order lands in DRAFT by default; staff can
    # still pick any other node from the stepper.
    reopen = _node(OrderStatus.DRAFT) if is_cancelled and OrderStatus.DRAFT in allowed else None

    # A contact looking at a quote they cannot confirm yet deserves to
    # know why the button is missing.
    blocked_reason = None
    if not principal.is_staff and problems and order.status == OrderStatus.QUOTED:
        blocked_reason = _t(
            request, "The supplier has not priced every item yet, so the quote cannot be confirmed."
        )

    # Targets for the staff "change status with a note" form (LOGIC-22).
    note_targets = (
        [
            {"value": s.value, "label": _label(s)}
            for s in (*PIPELINE, OrderStatus.CANCELLED)
            if s in allowed
        ]
        if principal.is_staff
        else []
    )

    return {
        "nodes": nodes,
        "primary": primary,
        "cancel": cancel,
        "reopen": reopen,
        "is_cancelled": is_cancelled,
        "current": current.value,
        "current_label": _label(current),
        "has_actions": bool(allowed),
        "blocked_reason": blocked_reason,
        "note_targets": note_targets,
        "has_quote_problems": bool(problems),
        "expected_total": (
            f"{Decimal(order.quoted_total):.2f}" if order.quoted_total is not None else ""
        ),
    }


# ---------------------------------------------------------------- add item


def _order_redirect(order_id: UUID, *, notice: str | None = None, error: str | None = None):
    """POST-redirect-GET back to the order detail with a flash (§9)."""
    if error is not None:
        return RedirectResponse(url=f"/app/orders/{order_id}?error={quote(error)}", status_code=303)
    return RedirectResponse(
        url=f"/app/orders/{order_id}?notice={quote(notice or '')}", status_code=303
    )


async def _resolve_catalog_product(db: AsyncSession, *, product_id: UUID, order: Order):
    """Load a catalog product usable on ``order``, or ``None``.

    Scoped to active products that are shared or dedicated to the order's
    customer — for staff too (LOGIC-14): staff used to be able to attach
    a deactivated product, or customer B's product *with B's negotiated
    price*, to customer A's order. When the picked product is the shared
    row and the customer has its own row for the same SKU, the customer's
    row wins: that is the special price the supplier agreed with them.
    """
    from sqlalchemy import or_

    product = (
        await db.execute(
            select(Product).where(
                Product.id == product_id,
                Product.is_active.is_(True),
                or_(Product.customer_id.is_(None), Product.customer_id == order.customer_id),
            )
        )
    ).scalar_one_or_none()
    if product is None or product.customer_id is not None:
        return product
    override = (
        await db.execute(
            select(Product).where(
                Product.sku == product.sku,
                Product.customer_id == order.customer_id,
                Product.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    return override or product


@router.post("/{order_id}/items", response_class=HTMLResponse)
async def orders_add_item(
    order_id: UUID,
    request: Request,
    description: str = Form(""),
    quantity: str = Form(""),
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
    from app.services.customer_permissions import OrderPermissions

    perms = OrderPermissions()
    if not principal.is_staff:
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

    # One parser for every money/quantity input (LOGIC-1, D4): NaN,
    # Infinity, negatives and out-of-range values are refused with a
    # flash instead of being stored — or 500-ing the page.
    try:
        qty = parse_quantity(quantity)
        price = parse_money(unit_price)
    except AmountError as exc:
        return _order_redirect(order.id, error=amount_error_message(request, exc))

    # Optional product link — when provided, look it up and back-fill the
    # line item's description/unit/price from the catalog unless the caller
    # overrode them in the form.
    product_uuid: UUID | None = None
    if product_id.strip():
        try:
            product_uuid = UUID(product_id.strip())
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid product_id") from None

        if not principal.is_staff and not perms.can_use_catalog:
            raise HTTPException(status_code=403, detail="Catalog is disabled for your account")

        product = await _resolve_catalog_product(db, product_id=product_uuid, order=order)
        if product is None:
            return _order_redirect(
                order.id, error=_t(request, "That product is not available for this order.")
            )
        product_uuid = product.id

        if not description.strip():
            description = f"{product.sku} — {product.name}"
        if not unit or unit == "ks":
            unit = product.unit
        if price is None and product.default_price is not None:
            # Deliberately NOT gated on can_set_prices: this is the
            # supplier's own list price being applied, not the contact
            # choosing a number. can_set_prices governs prices the
            # contact types in, which is checked on unit_price above.
            price = product.default_price

    if not description.strip():
        return _order_redirect(order.id, error=_t(request, "Enter an item description."))

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
            audit_actor=actor_from_principal(principal),
        )
    except ForbiddenTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except InvalidAmount as exc:
        return _order_redirect(order.id, error=amount_error_message(request, exc.amount_error))
    except OrderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    return _order_redirect(order.id, notice=_t(request, "Item added."))


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
            qty = parse_quantity(quantity)
        except AmountError as exc:
            row_error = amount_error_message(request, exc)

    if row_error is None and unit_price.strip():
        if not can_set_prices:
            raise HTTPException(
                status_code=403, detail="Setting prices is disabled for your account"
            )
        try:
            price = parse_money(unit_price)
        except AmountError as exc:
            row_error = amount_error_message(request, exc)

    # The note field is a free-form string; empty-string means "clear".
    # The caller sends it on every change because the whole row is
    # serialised (hx-include="closest tr"); we distinguish "blank was
    # typed" from "field was absent" by always applying it when the form
    # field exists at all.
    if "note" in (await request.form()):
        note_value = note

    status_code = 200
    if row_error is None:
        # Wrap the mutation in a SAVEPOINT so a domain error (e.g. the
        # order transitioning out of DRAFT mid-autosave) can be rolled
        # back cleanly without killing the request-scoped outer
        # transaction owned by ``get_db``.
        savepoint = await db.begin_nested()
        try:
            await update_item(
                db,
                order=order,
                item=item,
                quantity=qty,
                unit_price=price,
                note=note_value,
                actor=_actor(principal),
                audit_actor=actor_from_principal(principal),
            )
            await savepoint.commit()
        except ForbiddenTransition:
            await savepoint.rollback()
            row_error = _t(request, "This order can no longer be edited.")
            status_code = 409
        except OrderAccessDenied:
            await savepoint.rollback()
            raise HTTPException(status_code=404, detail="Order not found") from None
        except InvalidAmount as exc:
            await savepoint.rollback()
            row_error = amount_error_message(request, exc.amount_error)
        except OrderError as exc:
            await savepoint.rollback()
            row_error = str(exc)

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
        await remove_item(
            db,
            order=order,
            item=item,
            actor=_actor(principal),
            audit_actor=actor_from_principal(principal),
        )
    except ForbiddenTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None

    notice = quote(_t(request, "Item deleted."))
    return RedirectResponse(url=f"/app/orders/{order.id}?notice={notice}", status_code=303)


# ------------------------------------------------- "notify the customer" opt-out

#: Staff moves that never e-mail the customer anyway: SUBMITTED notifies
#: the supplier's own staff, and back to DRAFT is an internal correction
#: (LOGIC-22). The "Notify the customer" opt-out is moot for these, so
#: nothing is recorded about it.
_NO_CUSTOMER_MAIL_TARGETS = frozenset({OrderStatus.SUBMITTED, OrderStatus.DRAFT})


def _customer_mail_suppressed(
    principal: Principal, target: OrderStatus, notify_customer: list[str]
) -> bool:
    """Did a staff member untick "Notify the customer by e-mail"?

    The forms render a hidden ``notify_customer=0`` before the checkbox
    (``value=1``, checked by default), so the field is present either
    way. An absent field — the stepper's one-click buttons, a contact's
    own action, a scripted POST — keeps today's behaviour: notify.

    This is a sender-side choice per action. It can only take the
    customer side *out* of a mail; it never adds recipients, and the
    consent rules in ``notification_service`` (CLAUDE.md §19) still
    decide who is mailed when it is left on.
    """
    if not principal.is_staff or target in _NO_CUSTOMER_MAIL_TARGETS:
        return False
    return bool(notify_customer) and "1" not in notify_customer


# ----------------------------------------------------------- bulk transition


#: Upper bound on one bulk status change (BE-21).
BULK_MAX_ORDERS = 200


@router.post("/bulk/transition")
async def orders_bulk_transition(
    request: Request,
    background_tasks: BackgroundTasks,
    order_ids: list[str] = Form(default=[]),
    to_status: str = Form(...),
    notify_customer: list[str] = Form(default=[]),
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Transition a batch of orders to the same target status (staff only).

    Accepts a repeated ``order_ids`` form field and a single ``to_status``.
    Each order is passed through :func:`transition_order` (which owns the
    state-machine, history write, and timestamp side effects). Per-order
    errors are aggregated into a flash summary; orders that transitioned
    successfully have a status-change notification dispatched after commit.
    """
    # Validate the target status up-front; invalid input redirects back with
    # an error flash instead of 400-ing — the user can retry from the list.
    try:
        target = OrderStatus(to_status)
    except ValueError:
        return RedirectResponse(
            url=f"/app/orders?error={_t(request, 'Unknown status')}",
            status_code=303,
        )

    # Parse + de-duplicate UUID inputs. Malformed IDs are silently dropped
    # — the UI only ever sends valid IDs; we do not want one tampered
    # field to block the rest.
    parsed_ids: list[UUID] = []
    seen: set[UUID] = set()
    for raw in order_ids:
        try:
            uid = UUID(raw)
        except (ValueError, AttributeError):
            continue
        if uid in seen:
            continue
        seen.add(uid)
        parsed_ids.append(uid)

    if not parsed_ids:
        return RedirectResponse(
            url=f"/app/orders?error={_t(request, 'Select at least one order.')}",
            status_code=303,
        )
    if len(parsed_ids) > BULK_MAX_ORDERS:
        # BE-21: every order is locked FOR UPDATE until commit and fans
        # out recipient queries; an unbounded list is a self-inflicted
        # outage. The UI selects at most one page (20).
        msg = _t(request, "Select at most {count} orders at once.").format(count=BULK_MAX_ORDERS)
        return RedirectResponse(url=f"/app/orders?error={quote(msg)}", status_code=303)

    # Defence-in-depth: RLS already restricts the tenant; filter by the
    # submitted IDs explicitly so we never act on anything not requested.
    orders = list((await db.execute(select(Order).where(Order.id.in_(parsed_ids)))).scalars().all())

    silenced = _customer_mail_suppressed(principal, target, notify_customer)
    result = await bulk_transition(
        db,
        orders=orders,
        to_status=target,
        actor=_actor(principal),
        audit_actor=actor_from_principal(principal),
        notify_customer=not silenced,
        not_notified_note=_t(request, "Customer not notified by e-mail."),
    )

    # Build notification payloads while the session is still open — RLS
    # scoping is active here. Commit BEFORE scheduling background tasks
    # so their fresh session can read the new state (CLAUDE.md §2).
    tenant = request.state.tenant
    settings = request.app.state.settings
    sender = request.app.state.email_sender
    from app.urls import tenant_base_url

    tenant_url = tenant_base_url(settings, tenant)

    notifications: list = []
    orders_by_id = {o.id: o for o in orders}
    # One customer lookup per batch instead of one per order (BE-21).
    customer_ids = {o.customer_id for o in orders}
    customers_by_id = {
        c.id: c
        for c in (await db.execute(select(Customer).where(Customer.id.in_(customer_ids)))).scalars()
    }
    for order_id in result.succeeded:
        order = orders_by_id.get(order_id)
        if order is None:
            continue
        if target == OrderStatus.SUBMITTED:
            notifications.extend(
                await build_order_submitted(
                    db,
                    tenant=tenant,
                    order=order,
                    base_url=tenant_url,
                    settings=settings,
                    actor_email=principal.email,
                )
            )
        elif target == OrderStatus.DRAFT:
            # Back to draft is an internal correction (LOGIC-22) — same
            # rule as the single-order route: the customer is not mailed.
            continue
        elif silenced:
            # "Notify the customer" unticked: this route only ever mails
            # the customer side, so there is nobody left to tell.
            continue
        else:
            notifications.extend(
                await build_order_status_changed(
                    db,
                    tenant=tenant,
                    order=order,
                    to_status=target,
                    base_url=tenant_url,
                    # Staff-only route (``require_tenant_staff``), so the
                    # audience is always the customer side.
                    actor_is_contact=False,
                    actor_email=principal.email,
                    settings=settings,
                    customer=customers_by_id.get(order.customer_id),
                )
            )

    # Collapse per-order payloads into one mail per recipient. Moving 30
    # orders used to send 30 separate emails to every recipient; now it
    # sends one listing all 30. Recipients with a single order keep the
    # normal single-order template.
    batched = merge_for_digest(notifications)

    await db.commit()

    if batched:
        from app.tasks.email_tasks import send_order_notifications

        background_tasks.add_task(send_order_notifications, sender, batched)

    ok = len(result.succeeded)
    failed = len(result.errors)
    # Flash summary — localised, but the numeric template is shared.
    quiet = " " + _t(request, "Customer not notified by e-mail.") if silenced and ok else ""
    if failed == 0:
        notice = _t(request, "{count} orders transitioned.").format(count=ok) + quiet
        return RedirectResponse(url=f"/app/orders?notice={notice}", status_code=303)
    summary = _t(request, "{ok} transitioned, {failed} failed.").format(ok=ok, failed=failed)
    summary += quiet
    # Treat partial failure as a notice (not error) so successful rows
    # still get positive confirmation; the "failed" count tells the staff
    # member to inspect those orders individually.
    return RedirectResponse(url=f"/app/orders?notice={summary}", status_code=303)


# --------------------------------------------------------------- transition


def _parse_expected_total(raw: str) -> Decimal | None:
    """The total the confirming page rendered; ``""`` = rendered unpriced."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except (ArithmeticError, ValueError):
        return Decimal("-1")  # never equal to a real total -> "quote changed"
    return value if value.is_finite() else Decimal("-1")


def _quote_problem_message(request: Request, code: str) -> str:
    if code == "no_items":
        return _t(request, "Add at least one item before sending or confirming the quote.")
    return _t(request, "Every item needs a price before the quote can be sent or confirmed.")


@router.post("/{order_id}/transitions/{to_status}", response_class=HTMLResponse)
async def orders_transition(
    order_id: UUID,
    to_status: str,
    request: Request,
    background_tasks: BackgroundTasks,
    note: str = Form(""),
    promised_delivery_at: str = Form(""),
    expected_total: str | None = Form(None),
    allow_incomplete: str = Form(""),
    notify_customer: list[str] = Form(default=[]),
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

    # Optimistic check on the agreed amount (LOGIC-2). The confirm form
    # posts the total it rendered; a contact *must* send it, so a stale
    # tab or a crafted POST can never accept a price nobody displayed.
    check: object = NO_TOTAL_CHECK
    if target == OrderStatus.CONFIRMED:
        if expected_total is not None:
            check = _parse_expected_total(expected_total)
        elif not principal.is_staff:
            return _order_redirect(
                order.id,
                error=_t(request, "The quote has changed. Please review it and confirm again."),
            )

    promised: date | None = None
    if principal.is_staff and promised_delivery_at.strip():
        try:
            promised = date.fromisoformat(promised_delivery_at.strip())
        except ValueError:
            return _order_redirect(order.id, error=_t(request, "Enter a valid date."))

    clean_note = note.strip()[:1000] if principal.is_staff else ""
    silenced = _customer_mail_suppressed(principal, target, notify_customer)

    try:
        await transition_order(
            db,
            order=order,
            to_status=target,
            actor=_actor(principal),
            audit_actor=actor_from_principal(principal),
            note=clean_note or None,
            expected_total=check,
            allow_incomplete=principal.is_staff and bool(allow_incomplete),
            promised_delivery_at=promised,
            incomplete_note=_t(request, "Sent although some items had no price."),
            notify_customer=not silenced,
            not_notified_note=_t(request, "Customer not notified by e-mail."),
        )
    except QuoteChanged:
        return _order_redirect(
            order.id,
            error=_t(request, "The quote has changed. Please review it and confirm again."),
        )
    except IncompleteQuote as exc:
        return _order_redirect(order.id, error=_quote_problem_message(request, str(exc)))
    except EmptyOrder:
        return _order_redirect(
            order.id, error=_t(request, "Add at least one item before submitting the order.")
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

    notifications: list = []
    if target == OrderStatus.SUBMITTED:
        notifications = await build_order_submitted(
            db,
            tenant=tenant,
            order=order,
            base_url=tenant_url,
            settings=settings,
            actor_email=principal.email,
        )
    elif principal.is_staff and target == OrderStatus.DRAFT:
        # Moving an order back to draft is an internal correction
        # (LOGIC-22): the customer was told "Draft" for every mis-click
        # fix, which reads as "your order was thrown away".
        notifications = []
    elif silenced:
        # "Notify the customer by e-mail" unticked (LOGIC-22). A staff
        # move to any status but SUBMITTED mails only the customer side,
        # so suppressing it leaves the staff notifications untouched.
        notifications = []
    else:
        notifications = await build_order_status_changed(
            db,
            tenant=tenant,
            order=order,
            to_status=target,
            base_url=tenant_url,
            actor_is_contact=principal.type == "contact",
            actor_email=principal.email,
            settings=settings,
            note=clean_note or None,
        )

    # Commit so the background task's fresh session can see the new state.
    await db.commit()

    if notifications:
        from app.tasks.email_tasks import send_order_notifications

        background_tasks.add_task(send_order_notifications, sender, notifications)

    # Flash so the user gets a confirmation — silent redirects leave
    # them wondering if the click did anything. Localised via _t() and
    # the STATUS_LABELS *noun* so "in_production" → "Ve výrobě" (cs) /
    # "In production" (en). It used to read the transition *verb* here,
    # which produced "Status changed to Start production."
    status_label = _t(request, STATUS_LABELS.get(target, target.value))
    notice_msg = _t(request, "Status changed to {status}.").format(status=status_label)
    if silenced:
        notice_msg += " " + _t(request, "Customer not notified by e-mail.")
    return _order_redirect(order.id, notice=notice_msg)


@router.post("/{order_id}/status", response_class=HTMLResponse)
async def orders_transition_with_note(
    order_id: UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    to_status: str = Form(...),
    note: str = Form(""),
    promised_delivery_at: str = Form(""),
    expected_total: str | None = Form(None),
    allow_incomplete: str = Form(""),
    notify_customer: list[str] = Form(default=[]),
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Staff status change with an optional reason (LOGIC-22).

    The stepper's one-click nodes cannot carry a text field each, so the
    "Change status with a note" form posts the target as a field and
    shares every rule with :func:`orders_transition`.
    """
    return await orders_transition(
        order_id=order_id,
        to_status=to_status,
        request=request,
        background_tasks=background_tasks,
        note=note,
        promised_delivery_at=promised_delivery_at,
        expected_total=expected_total,
        allow_incomplete=allow_incomplete,
        notify_customer=notify_customer,
        principal=principal,
        db=db,
    )


# ------------------------------------------------------------- header edit


async def _render_edit_form(
    request: Request,
    db: AsyncSession,
    principal: Principal,
    order: Order,
    *,
    form: dict,
    error: str | None = None,
) -> HTMLResponse:
    from app.services.order_service import CUSTOMER_EDIT_STATES

    html = _templates(request).render(
        request,
        "orders/edit.html",
        {
            "principal": principal,
            "tenant": _tenant(request),
            "order": order,
            "customers": await list_customers(db),
            "can_change_customer": order.status in CUSTOMER_EDIT_STATES,
            "form": form,
            "error": error,
            "notice": None,
        },
    )
    return HTMLResponse(html, status_code=400 if error else 200)


@router.get("/{order_id}/edit", response_class=HTMLResponse)
async def orders_edit_form(
    order_id: UUID,
    request: Request,
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    try:
        order = await get_order_for_principal(db, order_id=order_id, actor=_actor(principal))
    except (OrderNotFound, OrderAccessDenied):
        raise HTTPException(status_code=404, detail="Order not found") from None
    return await _render_edit_form(
        request,
        db,
        principal,
        order,
        form={
            "title": order.title,
            "customer_id": str(order.customer_id),
            "requested_delivery_at": (
                order.requested_delivery_at.isoformat() if order.requested_delivery_at else ""
            ),
            "promised_delivery_at": (
                order.promised_delivery_at.isoformat() if order.promised_delivery_at else ""
            ),
            "notes": order.notes or "",
        },
    )


@router.post("/{order_id}/edit", response_class=HTMLResponse)
async def orders_edit(
    order_id: UUID,
    request: Request,
    title: str = Form(""),
    customer_id: str = Form(""),
    requested_delivery_at: str = Form(""),
    promised_delivery_at: str = Form(""),
    notes: str = Form(""),
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Correct the order header (LOGIC-12, IDEA-4). Staff only, audited."""
    try:
        order = await get_order_for_principal(db, order_id=order_id, actor=_actor(principal))
    except (OrderNotFound, OrderAccessDenied):
        raise HTTPException(status_code=404, detail="Order not found") from None

    form = {
        "title": title,
        "customer_id": customer_id,
        "requested_delivery_at": requested_delivery_at,
        "promised_delivery_at": promised_delivery_at,
        "notes": notes,
    }

    def _date(raw: str) -> date | None:
        return date.fromisoformat(raw.strip()) if raw.strip() else None

    try:
        requested = _date(requested_delivery_at)
        promised = _date(promised_delivery_at)
    except ValueError:
        return await _render_edit_form(
            request, db, principal, order, form=form, error=_t(request, "Enter a valid date.")
        )

    target_customer = order.customer_id
    if customer_id.strip():
        try:
            target_customer = UUID(customer_id.strip())
        except ValueError:
            return await _render_edit_form(
                request, db, principal, order, form=form, error=_t(request, "Choose a client.")
            )

    try:
        changed = await update_order_header(
            db,
            order=order,
            actor=_actor(principal),
            title=title,
            customer_id=target_customer,
            requested_delivery_at=requested,
            promised_delivery_at=promised,
            notes=notes,
            audit_actor=actor_from_principal(principal),
        )
    except ForbiddenTransition:
        return await _render_edit_form(
            request,
            db,
            principal,
            order,
            form=form,
            error=_t(request, "The client can only be changed before the order is quoted."),
        )
    except OrderError:
        return await _render_edit_form(
            request,
            db,
            principal,
            order,
            form=form,
            error=_t(request, "Fill in the name and choose an existing client."),
        )

    notice = _t(request, "Order updated.") if changed else _t(request, "No changes.")
    return _order_redirect(order.id, notice=notice)


# ------------------------------------------------------------- order again


@router.post("/{order_id}/duplicate", response_class=HTMLResponse)
async def orders_duplicate(
    order_id: UUID,
    request: Request,
    principal: Principal = Depends(require_login),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """ "Order again" (IDEA-3): a new DRAFT with the same lines.

    Available to staff and to the customer's own contacts (within their
    add-item permission). Prices are re-taken from the current catalog or
    left blank for a fresh quote — never copied from the old order.
    """
    try:
        source = await get_order_for_principal(db, order_id=order_id, actor=_actor(principal))
    except (OrderNotFound, OrderAccessDenied):
        raise HTTPException(status_code=404, detail="Order not found") from None

    include_catalog = True
    if not principal.is_staff:
        from app.services.customer_permissions import OrderPermissions

        customer = (
            await db.execute(select(Customer).where(Customer.id == source.customer_id))
        ).scalar_one_or_none()
        perms = OrderPermissions.from_dict(customer.order_permissions if customer else None)
        if not perms.can_add_items:
            return _order_redirect(
                source.id, error=_t(request, "Adding items is disabled for your account.")
            )
        include_catalog = perms.can_use_catalog

    new_order = await duplicate_order(
        db,
        tenant_id=principal.tenant_id,
        source=source,
        actor=_actor(principal),
        include_catalog=include_catalog,
        audit_actor=actor_from_principal(principal),
    )
    return _order_redirect(
        new_order.id,
        notice=_t(
            request, "New draft created from order {number}. Check the items and prices."
        ).format(number=source.number),
    )


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
            audit_actor=actor_from_principal(principal),
        )
    except ForbiddenActor as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except OrderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    # Build comment notification while the session is still open; skip
    # internal comments entirely (those are staff-only).
    notifications: list = []
    if not internal_flag:
        from app.services.notification_service import build_order_comment

        tenant = request.state.tenant
        settings = request.app.state.settings
        from app.urls import tenant_base_url

        notifications = await build_order_comment(
            db,
            tenant=tenant,
            order=order,
            author_email=principal.email,
            author_name=principal.full_name,
            author_is_staff=principal.is_staff,
            body=body,
            base_url=tenant_base_url(settings, tenant),
            settings=settings,
        )

    await db.commit()

    if notifications:
        from app.tasks.email_tasks import send_order_notifications

        background_tasks.add_task(
            send_order_notifications, request.app.state.email_sender, notifications
        )

    notice = quote(_t(request, "Comment added."))
    return RedirectResponse(url=f"/app/orders/{order.id}?notice={notice}", status_code=303)


@router.post("/{order_id}/assign", response_class=HTMLResponse)
async def orders_assign(
    order_id: UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    assigned_to: str = Form(""),
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Set or clear the staff member responsible for an order.

    Staff only — ``require_tenant_staff`` keeps contacts out entirely, so
    a customer can neither see nor change who is working on their job.
    An empty ``assigned_to`` unassigns.
    """
    try:
        order = await get_order_for_principal(db, order_id=order_id, actor=_actor(principal))
    except (OrderNotFound, OrderAccessDenied):
        raise HTTPException(status_code=404, detail="Order not found") from None

    assignee_id: UUID | None = None
    if assigned_to.strip():
        try:
            assignee_id = UUID(assigned_to.strip())
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid assignee") from None

    try:
        assignee = await assign_order(
            db,
            order=order,
            assignee_id=assignee_id,
            actor=_actor(principal),
            audit_actor=actor_from_principal(principal),
        )
    except ForbiddenActor as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except OrderError:
        return RedirectResponse(
            url=f"/app/orders/{order.id}?error="
            + quote(_t(request, "That person can no longer be assigned work.")),
            status_code=303,
        )

    # ``assign_order`` returns None both for "unassigned" and for "no
    # change"; either way there is nobody new to email.
    notifications: list = []
    if assignee is not None:
        from app.services.notification_service import build_order_assigned
        from app.urls import tenant_base_url

        tenant = request.state.tenant
        settings = request.app.state.settings
        notifications = build_order_assigned(
            tenant=tenant,
            order=order,
            assignee=assignee,
            base_url=tenant_base_url(settings, tenant),
            settings=settings,
            actor_email=principal.email,
            actor_name=principal.full_name,
        )

    await db.commit()

    if notifications:
        from app.tasks.email_tasks import send_order_notifications

        background_tasks.add_task(
            send_order_notifications, request.app.state.email_sender, notifications
        )

    if assignee is not None:
        notice = _t(request, "Assigned to {name}.").format(name=assignee.full_name)
    else:
        notice = _t(request, "Assignment updated.")
    return RedirectResponse(url=f"/app/orders/{order.id}?notice={quote(notice)}", status_code=303)
