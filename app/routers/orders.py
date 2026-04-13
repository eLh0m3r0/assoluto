"""Orders routes: list, new, detail, items, transitions, comments."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import Principal, get_db, require_login
from app.models.customer import Customer
from app.models.enums import OrderStatus
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
    create_order,
    get_order_for_principal,
    list_comments,
    list_items,
    list_orders_for_principal,
    list_status_history,
    remove_item,
    transition_order,
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
            "status_choices": [
                ("", "Všechny stavy"),
                ("draft", "Koncept"),
                ("submitted", "Odesláno"),
                ("quoted", "Nacenění"),
                ("confirmed", "Potvrzeno"),
                ("in_production", "Ve výrobě"),
                ("ready", "Připraveno"),
                ("delivered", "Dodáno"),
                ("closed", "Uzavřeno"),
                ("cancelled", "Zrušeno"),
            ],
        },
    )
    return HTMLResponse(html)


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
                error="Vyberte klienta.",
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

    customer = None
    if principal.is_staff:
        customer = (
            await db.execute(select(Customer).where(Customer.id == order.customer_id))
        ).scalar_one_or_none()

    available_transitions = _available_transitions(order, principal)

    # Products available on this order (shared + customer-specific) — used
    # by the "Add item" picker. Only loaded while items are still editable.
    product_choices: list = []
    if _can_edit_items(order, principal):
        product_choices = await search_products(
            db, query="", customer_id=order.customer_id, limit=200
        )

    html = _templates(request).render(
        request,
        "orders/detail.html",
        {
            "principal": principal,
            "tenant": _tenant(request),
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


TRANSITION_LABELS: dict[OrderStatus, str] = {
    OrderStatus.SUBMITTED: "Odeslat",
    OrderStatus.QUOTED: "Poslat nacenění",
    OrderStatus.CONFIRMED: "Potvrdit",
    OrderStatus.IN_PRODUCTION: "Spustit výrobu",
    OrderStatus.READY: "Označit připraveno",
    OrderStatus.DELIVERED: "Dodáno",
    OrderStatus.CLOSED: "Uzavřít",
    OrderStatus.CANCELLED: "Zrušit",
}


def _available_transitions(order, principal: Principal) -> list[dict]:
    """Return the list of transitions the current principal can perform.

    Staff see every status except the current one — full admin control.
    Contacts see only what the state machine allows.
    """
    from app.services.order_service import ALL_STATUSES, CONTACT_ALLOWED_TRANSITIONS

    out: list[dict] = []
    if principal.is_staff:
        candidates = ALL_STATUSES - {order.status}
    else:
        candidates = CONTACT_ALLOWED_TRANSITIONS.get(order.status, set())

    for to_status in candidates:
        out.append(
            {
                "to_status": to_status.value,
                "label": TRANSITION_LABELS.get(to_status, to_status.value),
            }
        )
    # Stable order: cancelled always last.
    out.sort(key=lambda x: (x["to_status"] == "cancelled", x["label"]))
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

    notif_submitted = None
    notif_status = None
    if target == OrderStatus.SUBMITTED:
        notif_submitted = await build_order_submitted(
            db,
            tenant_name=tenant.name,
            order=order,
            base_url=settings.app_base_url,
        )
    else:
        notif_status = await build_order_status_changed(
            db,
            tenant_name=tenant.name,
            order=order,
            to_status=target,
            base_url=settings.app_base_url,
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
        notif = await build_order_comment(
            db,
            tenant_name=tenant.name,
            order=order,
            author_email=principal.email,
            author_name=principal.full_name,
            author_is_staff=principal.is_staff,
            body=body,
            base_url=settings.app_base_url,
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
