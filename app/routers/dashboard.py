"""Dashboard route — the rendez-vous point after login.

Tenant staff see live counts of customers, open orders, and assets.
Customer contacts see counts of their own open orders and assets.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import Principal, get_db, require_login
from app.i18n import get_translations
from app.i18n import t as _t
from app.models.asset import Asset
from app.models.customer import Customer
from app.models.enums import OrderStatus
from app.models.order import Order
from app.security.csrf import verify_csrf
from app.services import audit_service
from app.services.order_service import (
    DEFAULT_STALE_QUOTE_DAYS,
    OPEN_ORDER_STATUSES,
    WORK_QUEUES,
    ActorRef,
    list_orders_for_principal,
    work_queue_counts,
)
from app.timezones import local_today, request_tz

router = APIRouter(prefix="/app", tags=["dashboard"], dependencies=[Depends(verify_csrf)])


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
    # Drafts are not open orders (P3-14) — they are counted on the side so
    # the card can say how many it leaves out.
    order_stmt = select(
        func.count().filter(Order.status.in_(OPEN_ORDER_STATUSES)),
        func.count().filter(Order.status == OrderStatus.DRAFT),
    ).select_from(Order)
    if not principal.is_staff:
        order_stmt = order_stmt.where(Order.customer_id == principal.customer_id)
    open_count, draft_count = (await db.execute(order_stmt)).one()
    stats["open_orders"] = int(open_count or 0)
    stats["drafts"] = int(draft_count or 0)

    # Active assets: same scoping.
    asset_stmt = select(func.count()).select_from(Asset).where(Asset.is_active.is_(True))
    if not principal.is_staff:
        asset_stmt = asset_stmt.where(Asset.customer_id == principal.customer_id)
    stats["assets"] = int((await db.execute(asset_stmt)).scalar() or 0)

    if principal.is_staff:
        stats["customers"] = int(
            (await db.execute(select(func.count()).select_from(Customer))).scalar() or 0
        )

    # "Needs action" (IDEA-1 / UX-12): the four questions a supplier
    # answers every morning by scanning the list by eye. Each count links
    # to the order list filtered by the same predicate, so the number and
    # the list behind it can never disagree. Contacts get their own
    # single queue: quotes waiting for *their* confirmation.
    work_queues: list[dict] = []
    if principal.is_staff:
        settings = request.app.state.settings
        stale_days = int(getattr(settings, "quote_reminder_days", 0) or DEFAULT_STALE_QUOTE_DAYS)
        counts = await work_queue_counts(
            db, stale_quote_days=stale_days, today=local_today(request_tz(request))
        )
        labels = {
            "awaiting_quote": _t(request, "Submitted, waiting for a quote"),
            "no_promise": _t(request, "Confirmed without a promised date"),
            "overdue": _t(request, "Overdue"),
            "stale_quotes": _t(request, "Quotes waiting for the client"),
        }
        # Plural-aware: "older than 1 day / 3 days" (cs: den / dny / dní).
        # Bound to the name ``ngettext`` so the canonical extract keyword
        # ``ngettext:1,2`` picks the msgids up (CLAUDE.md §7).
        ngettext = get_translations(getattr(request.state, "locale", None) or "cs").ngettext
        hints = {
            "awaiting_quote": _t(request, "Price them and send the quote."),
            "no_promise": _t(request, "Promise the client a delivery date."),
            "overdue": _t(request, "The promised date has passed."),
            "stale_quotes": ngettext(
                "Older than {days} day — follow up.",
                "Older than {days} days — follow up.",
                stale_days,
            ).format(days=stale_days),
        }
        work_queues = [
            {
                "key": key,
                "label": labels[key],
                "hint": hints[key],
                "count": counts[key],
                "url": f"/app/orders?queue={key}",
                "urgent": key == "overdue" and counts[key] > 0,
            }
            for key in WORK_QUEUES
        ]
    else:
        awaiting = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(Order)
                    .where(
                        Order.customer_id == principal.customer_id,
                        Order.status == OrderStatus.QUOTED,
                    )
                )
            ).scalar()
            or 0
        )
        work_queues = [
            {
                "key": "awaiting_confirmation",
                "label": _t(request, "Quotes waiting for your confirmation"),
                "hint": _t(request, "Review the price and confirm the order."),
                "count": awaiting,
                "url": "/app/orders?status=quoted",
                "urgent": awaiting > 0,
            }
        ]

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

    # P3-8: an upload event is about a file, but the reader wants to know
    # which *order* got it. The order id sits in the event's diff.
    activity_orders = await _upload_event_orders(db, recent_activity, principal)

    # Public demo (P2-14): a "Where to start" card for the supplier.
    demo_start = None
    if principal.is_staff and getattr(request.state, "public_demo", False):
        from app.demo.landing import start_here_links

        demo_start = await start_here_links(db, today=local_today(request_tz(request)))

    html = _templates(request).render(
        request,
        "dashboard/index.html",
        {
            "principal": principal,
            "tenant": tenant,
            "stats": stats,
            "recent_orders": recent_orders,
            "work_queues": work_queues,
            "customer_by_id": customer_by_id,
            "recent_activity": recent_activity,
            "activity_orders": activity_orders,
            "demo_start": demo_start,
        },
    )
    return HTMLResponse(html)


async def _upload_event_orders(
    db: AsyncSession, events: list, principal: Principal
) -> dict[str, Order]:
    """``{order id (str): Order}`` for the ``attachment.upload`` events shown."""
    ids: set[UUID] = set()
    for event in events:
        if event.action != "attachment.upload" or not isinstance(event.diff, dict):
            continue
        raw = (event.diff.get("after") or {}).get("order_id")
        try:
            ids.add(UUID(str(raw)))
        except (TypeError, ValueError):
            continue
    if not ids:
        return {}
    stmt = select(Order).where(Order.id.in_(ids))
    if not principal.is_staff:
        stmt = stmt.where(Order.customer_id == principal.customer_id)
    return {str(o.id): o for o in (await db.execute(stmt)).scalars().all()}
