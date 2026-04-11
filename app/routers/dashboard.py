"""Dashboard route — the rendez-vous point after login.

Tenant staff see live counts of customers, open orders, and assets.
Customer contacts see counts of their own open orders and assets.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import Principal, get_db, require_login
from app.models.asset import Asset
from app.models.customer import Customer
from app.models.enums import OrderStatus
from app.models.order import Order

router = APIRouter(prefix="/app", tags=["dashboard"])


# Any order the portal still expects a human action on.
OPEN_ORDER_STATUSES = (
    OrderStatus.DRAFT,
    OrderStatus.SUBMITTED,
    OrderStatus.QUOTED,
    OrderStatus.CONFIRMED,
    OrderStatus.IN_PRODUCTION,
    OrderStatus.READY,
)


def _templates(request: Request):
    return request.app.state.templates


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard_index(
    request: Request,
    principal: Principal = Depends(require_login),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    tenant = getattr(request.state, "tenant", None)
    if tenant is None:
        raise HTTPException(status_code=500, detail="Tenant not resolved")

    stats: dict[str, int] = {}

    # Open orders: staff see all; contacts see only their own customer's.
    order_stmt = (
        select(func.count()).select_from(Order).where(Order.status.in_(OPEN_ORDER_STATUSES))
    )
    if not principal.is_staff:
        order_stmt = order_stmt.where(Order.customer_id == principal.customer_id)
    stats["open_orders"] = int((await db.execute(order_stmt)).scalar() or 0)

    # Active assets: same scoping.
    asset_stmt = select(func.count()).select_from(Asset).where(Asset.is_active.is_(True))
    if not principal.is_staff:
        asset_stmt = asset_stmt.where(Asset.customer_id == principal.customer_id)
    stats["assets"] = int((await db.execute(asset_stmt)).scalar() or 0)

    if principal.is_staff:
        stats["customers"] = int(
            (await db.execute(select(func.count()).select_from(Customer))).scalar() or 0
        )

    html = _templates(request).render(
        request,
        "dashboard/index.html",
        {
            "principal": principal,
            "tenant": tenant,
            "stats": stats,
        },
    )
    return HTMLResponse(html)
