"""Asset routes: staff manages everything; contacts get read-only view of
their own customer's assets.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import Principal, get_db, require_login, require_tenant_staff
from app.i18n import t as _t
from app.models.customer import Customer
from app.models.enums import AssetMovementType
from app.security.csrf import verify_csrf
from app.services.asset_service import (
    AssetError,
    ForeignOrderReference,
    InsufficientStock,
    add_movement,
    create_asset,
    get_asset,
    list_assets,
    list_movements,
)
from app.services.audit_service import actor_from_principal
from app.services.customer_service import list_customers
from app.services.money import QUANTITY_MAX

router = APIRouter(prefix="/app", tags=["assets"], dependencies=[Depends(verify_csrf)])


def _templates(request: Request):
    return request.app.state.templates


def _tenant(request: Request):
    tenant = getattr(request.state, "tenant", None)
    if tenant is None:
        raise HTTPException(status_code=500, detail="Tenant not resolved")
    return tenant


@router.get("/assets", response_class=HTMLResponse)
async def assets_index(
    request: Request,
    principal: Principal = Depends(require_login),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    if principal.is_staff:
        assets = await list_assets(db)
        customers = await list_customers(db)
        customer_by_id = {c.id: c for c in customers}
    else:
        assets = await list_assets(db, customer_id=principal.customer_id)
        customer_by_id = {}

    html = _templates(request).render(
        request,
        "assets/list.html",
        {
            "principal": principal,
            "tenant": _tenant(request),
            "assets": assets,
            "customer_by_id": customer_by_id,
        },
    )
    return HTMLResponse(html)


@router.get("/assets/new", response_class=HTMLResponse)
async def assets_new_form(
    request: Request,
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    customers = await list_customers(db)
    html = _templates(request).render(
        request,
        "assets/form.html",
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


@router.post("/assets", response_class=HTMLResponse)
async def assets_create(
    request: Request,
    customer_id: str = Form(...),
    code: str = Form(...),
    name: str = Form(...),
    unit: str = Form("ks"),
    location: str = Form(""),
    description: str = Form(""),
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> Response:
    try:
        cust_uuid = UUID(customer_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid customer_id") from None

    try:
        asset = await create_asset(
            db,
            tenant_id=principal.tenant_id,
            customer_id=cust_uuid,
            code=code,
            name=name,
            unit=unit,
            description=description or None,
            location=location or None,
        )
    except (AssetError, IntegrityError) as exc:
        customers = await list_customers(db)
        error = (
            _t(request, "An asset with this code already exists for this client.")
            if isinstance(exc, IntegrityError)
            else str(exc)
        )
        html = _templates(request).render(
            request,
            "assets/form.html",
            {
                "principal": principal,
                "tenant": _tenant(request),
                "customers": customers,
                "form": {
                    "customer_id": customer_id,
                    "code": code,
                    "name": name,
                    "unit": unit,
                    "location": location,
                    "description": description,
                },
                "error": error,
                "notice": None,
            },
        )
        return HTMLResponse(html, status_code=400)

    return RedirectResponse(url=f"/app/assets/{asset.id}", status_code=303)


@router.get("/assets/{asset_id}", response_class=HTMLResponse)
async def assets_detail(
    asset_id: UUID,
    request: Request,
    notice: str | None = None,
    error: str | None = None,
    principal: Principal = Depends(require_login),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    asset = await get_asset(db, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")

    # Contacts may only see their own customer's assets.
    if not principal.is_staff and asset.customer_id != principal.customer_id:
        raise HTTPException(status_code=404, detail="Asset not found")

    from app.models.order import Order

    movements = await list_movements(db, asset_id=asset.id)
    # Order numbers for the movement table's "Order" column (P2-13).
    # Limited to the asset owner's orders, so a contact never sees a
    # number from another client even if a movement pointed elsewhere.
    ref_ids = {m.reference_order_id for m in movements if m.reference_order_id}
    movement_orders: dict = {}
    if ref_ids:
        movement_orders = {
            o.id: o
            for o in (
                await db.execute(
                    select(Order).where(
                        Order.id.in_(ref_ids), Order.customer_id == asset.customer_id
                    )
                )
            )
            .scalars()
            .all()
        }
    customer = None
    orders: list[Order] = []
    if principal.is_staff:
        customer = (
            await db.execute(select(Customer).where(Customer.id == asset.customer_id))
        ).scalar_one_or_none()

        orders = list(
            (
                await db.execute(
                    select(Order)
                    .where(Order.customer_id == asset.customer_id)
                    .order_by(Order.created_at.desc())
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )

    html = _templates(request).render(
        request,
        "assets/detail.html",
        {
            "principal": principal,
            "tenant": _tenant(request),
            "asset": asset,
            "movements": movements,
            "movement_orders": movement_orders,
            "customer": customer,
            "orders": orders,
            "error": error,
            "notice": notice,
        },
    )
    return HTMLResponse(html)


@router.post("/assets/{asset_id}/movements", response_class=HTMLResponse)
async def assets_add_movement(
    asset_id: UUID,
    request: Request,
    type: str = Form(...),
    quantity: str = Form(...),
    note: str = Form(""),
    reference_order_id: str = Form(""),
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> Response:
    asset = await get_asset(db, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")

    try:
        type_enum = AssetMovementType(type)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid movement type") from None

    def _back(*, notice: str | None = None, error: str | None = None) -> RedirectResponse:
        key, msg = ("error", error) if error is not None else ("notice", notice or "")
        return RedirectResponse(url=f"/app/assets/{asset.id}?{key}={quote(msg)}", status_code=303)

    try:
        qty = Decimal(quantity.strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return _back(error=_t(request, "Enter a valid quantity."))
    if not qty.is_finite() or abs(qty) > QUANTITY_MAX:
        return _back(error=_t(request, "Enter a valid quantity."))

    ref_order_uuid: UUID | None = None
    if reference_order_id.strip():
        try:
            ref_order_uuid = UUID(reference_order_id.strip())
        except ValueError:
            return _back(error=_t(request, "That order does not belong to this client."))

    try:
        await add_movement(
            db,
            tenant_id=principal.tenant_id,
            asset=asset,
            type_=type_enum,
            quantity=qty,
            note=note or None,
            reference_order_id=ref_order_uuid,
            created_by_user_id=principal.id,
            audit_actor=actor_from_principal(principal),
        )
    except InsufficientStock:
        return _back(error=_t(request, "Not enough stock for this movement."))
    except ForeignOrderReference:
        # LOGIC-24: a consume booked against another customer's order
        # (or a made-up id, which used to hit the FK and 500).
        return _back(error=_t(request, "That order does not belong to this client."))
    except AssetError:
        return _back(error=_t(request, "Enter a valid quantity."))

    return _back(notice=_t(request, "Movement recorded."))
