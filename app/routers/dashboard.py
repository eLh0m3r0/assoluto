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
from app.security.csrf import verify_csrf
from app.services import audit_service
from app.services.order_service import ActorRef, list_orders_for_principal

router = APIRouter(prefix="/app", tags=["dashboard"], dependencies=[Depends(verify_csrf)])


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

    # Recent orders: last 5 across all statuses (scoped for contacts via
    # ``list_orders_for_principal``). Used on the dashboard so the user
    # lands on something actionable rather than three bare counters.
    recent_orders, _ = await list_orders_for_principal(
        db,
        actor=ActorRef(
            type=principal.type,
            id=principal.id,
            customer_id=principal.customer_id,
        ),
        limit=5,
    )

    # Map customer_id → Customer for display; only needed for staff.
    customer_by_id: dict = {}
    if principal.is_staff and recent_orders:
        cust_ids = {o.customer_id for o in recent_orders}
        cust_rows = (
            (await db.execute(select(Customer).where(Customer.id.in_(cust_ids)))).scalars().all()
        )
        customer_by_id = {c.id: c for c in cust_rows}

    # Recent activity feed (§7) — reads from audit_events via the same
    # scoping rules as the audit log (staff see the whole tenant, contacts
    # only see order events on their own customer's orders).
    recent_activity = await audit_service.list_recent(db, principal=principal, limit=20)

    html = _templates(request).render(
        request,
        "dashboard/index.html",
        {
            "principal": principal,
            "tenant": tenant,
            "stats": stats,
            "recent_orders": recent_orders,
            "customer_by_id": customer_by_id,
            "recent_activity": recent_activity,
        },
    )
    return HTMLResponse(html)
