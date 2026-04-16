"""Platform admin: CRUD tenants, basic oversight.

Gated by `require_platform_admin`, so a regular Identity cannot see it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import Tenant
from app.platform.billing.models import Invoice, Subscription
from app.platform.deps import get_platform_db, require_platform_admin
from app.platform.models import Identity
from app.platform.service import (
    DuplicateTenantSlug,
    PlatformError,
    create_tenant_with_owner,
    deactivate_tenant,
    list_tenants,
)
from app.security.csrf import verify_csrf

router = APIRouter(
    prefix="/platform/admin",
    tags=["platform-admin"],
    dependencies=[Depends(verify_csrf)],
)


def _templates(request: Request):
    return request.app.state.templates


@router.get("/tenants", response_class=HTMLResponse)
async def tenants_index(
    request: Request,
    identity: Identity = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_platform_db),
) -> HTMLResponse:
    tenants = await list_tenants(db)
    html = _templates(request).render(
        request,
        "platform/admin/tenants.html",
        {
            "identity": identity,
            "tenants": tenants,
            "error": None,
            "notice": None,
            "principal": None,
        },
    )
    return HTMLResponse(html)


@router.post("/tenants", response_class=HTMLResponse)
async def tenants_create(
    request: Request,
    slug: str = Form(...),
    name: str = Form(...),
    owner_email: str = Form(...),
    owner_full_name: str = Form(...),
    owner_password: str = Form(...),
    identity: Identity = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_platform_db),
) -> Response:
    try:
        await create_tenant_with_owner(
            db,
            slug=slug,
            name=name,
            owner_email=owner_email,
            owner_full_name=owner_full_name,
            owner_password=owner_password,
        )
    except DuplicateTenantSlug:
        tenants = await list_tenants(db)
        html = _templates(request).render(
            request,
            "platform/admin/tenants.html",
            {
                "identity": identity,
                "tenants": tenants,
                "error": f"Tenant se slugem '{slug}' už existuje.",
                "notice": None,
                "principal": None,
            },
        )
        return HTMLResponse(html, status_code=400)
    except PlatformError as exc:
        tenants = await list_tenants(db)
        html = _templates(request).render(
            request,
            "platform/admin/tenants.html",
            {
                "identity": identity,
                "tenants": tenants,
                "error": str(exc),
                "notice": None,
                "principal": None,
            },
        )
        return HTMLResponse(html, status_code=400)

    await db.commit()
    return RedirectResponse(url="/platform/admin/tenants", status_code=303)


@router.get("/dashboard", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    identity: Identity = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_platform_db),
) -> HTMLResponse:
    """KPI dashboard for platform operators.

    Computes cheap aggregate metrics in one set of queries and renders
    a simple card layout. No heavy charting — Chart.js can be wired
    later if needed. The numbers here are intentionally the kind of
    "how's the business doing" signals you check twice a day.
    """
    now = datetime.now(UTC)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    total_tenants = int((await db.execute(select(func.count(Tenant.id)))).scalar_one())
    active_tenants = int(
        (
            await db.execute(select(func.count(Tenant.id)).where(Tenant.is_active.is_(True)))
        ).scalar_one()
    )
    signups_this_week = int(
        (
            await db.execute(select(func.count(Tenant.id)).where(Tenant.created_at >= week_ago))
        ).scalar_one()
    )
    signups_this_month = int(
        (
            await db.execute(select(func.count(Tenant.id)).where(Tenant.created_at >= month_ago))
        ).scalar_one()
    )

    subs_active = int(
        (
            await db.execute(
                select(func.count(Subscription.id)).where(
                    Subscription.status.in_(("active", "trialing", "demo"))
                )
            )
        ).scalar_one()
    )
    subs_trialing = int(
        (
            await db.execute(
                select(func.count(Subscription.id)).where(Subscription.status == "trialing")
            )
        ).scalar_one()
    )

    mrr_cents = int(
        (
            await db.execute(
                select(func.coalesce(func.sum(Invoice.amount_cents), 0))
                .where(Invoice.status == "paid")
                .where(Invoice.paid_at >= month_ago)
            )
        ).scalar_one()
    )

    recent_signups_q = select(Tenant).order_by(Tenant.created_at.desc()).limit(10)
    recent_signups = list((await db.execute(recent_signups_q)).scalars().all())

    html = _templates(request).render(
        request,
        "platform/admin/dashboard.html",
        {
            "identity": identity,
            "metrics": {
                "total_tenants": total_tenants,
                "active_tenants": active_tenants,
                "signups_this_week": signups_this_week,
                "signups_this_month": signups_this_month,
                "subs_active": subs_active,
                "subs_trialing": subs_trialing,
                "mrr_cents": mrr_cents,
                "mrr_currency": "CZK",
            },
            "recent_signups": recent_signups,
            "principal": None,
        },
    )
    return HTMLResponse(html)


@router.post("/tenants/{tenant_id}/deactivate")
async def tenants_deactivate(
    tenant_id: UUID,
    identity: Identity = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_platform_db),
) -> Response:
    try:
        await deactivate_tenant(db, tenant_id=tenant_id)
    except PlatformError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    await db.commit()
    return RedirectResponse(url="/platform/admin/tenants", status_code=303)
