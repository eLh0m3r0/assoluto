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

router = APIRouter(prefix="/app/orders", tags=["orders"])


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


@router.get("", response_class=HTMLResponse)
async def orders_index(
    request: Request,
    principal: Principal = Depends(require_login),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    orders = await list_orders_for_principal(db, actor=_actor(principal))

    customer_by_id: dict = {}
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
            "error": None,
            "notice": None,
        },
    )
    return HTMLResponse(html)


def _can_edit_items(order, principal: Principal) -> bool:
    if principal.is_staff:
        return order.status in (
            OrderStatus.DRAFT,
            OrderStatus.SUBMITTED,
            OrderStatus.QUOTED,
        )
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
    from app.services.order_service import ACTOR_RULES, ALLOWED_TRANSITIONS

    actor_type = principal.type
    out: list[dict] = []
    for to_status in ALLOWED_TRANSITIONS.get(order.status, set()):
        allowed = ACTOR_RULES.get((order.status, to_status), set())
        if actor_type in allowed:
            out.append(
                {
                    "to_status": to_status.value,
                    "label": TRANSITION_LABELS.get(to_status, to_status.value),
                }
            )
    # Stable order, cancel always last.
    out.sort(key=lambda x: (x["to_status"] == "cancelled", x["label"]))
    return out


# ---------------------------------------------------------------- add item


@router.post("/{order_id}/items", response_class=HTMLResponse)
async def orders_add_item(
    order_id: UUID,
    request: Request,
    description: str = Form(...),
    quantity: str = Form(...),
    unit: str = Form("ks"),
    unit_price: str = Form(""),
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
    body: str = Form(...),
    is_internal: str = Form(""),
    principal: Principal = Depends(require_login),
    db: AsyncSession = Depends(get_db),
) -> Response:
    try:
        order = await get_order_for_principal(db, order_id=order_id, actor=_actor(principal))
    except (OrderNotFound, OrderAccessDenied):
        raise HTTPException(status_code=404, detail="Order not found") from None

    try:
        await add_comment(
            db,
            tenant_id=principal.tenant_id,
            order=order,
            actor=_actor(principal),
            body=body,
            is_internal=bool(is_internal),
        )
    except ForbiddenActor as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except OrderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    return RedirectResponse(url=f"/app/orders/{order.id}", status_code=303)
