"""Platform admin: CRUD tenants, basic oversight.

Gated by `require_platform_admin`, so a regular Identity cannot see it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import Tenant
from app.platform.billing.models import Invoice, Plan, Subscription
from app.platform.deps import get_platform_db, require_platform_admin
from app.platform.models import Identity, TenantMembership
from app.platform.service import (
    DuplicateTenantSlug,
    PlatformError,
    create_tenant_with_owner,
    deactivate_tenant,
    grant_platform_admin_support_access,
    list_tenants,
    reactivate_tenant,
    revoke_platform_admin_support_access,
    update_tenant,
)
from app.security.csrf import verify_csrf


def _redir_tenants(notice: str | None = None, error: str | None = None) -> RedirectResponse:
    """Redirect to the tenants list with an optional flash message.

    POST-redirect-GET pattern: every mutating route should tell the
    user whether their action succeeded, not silently 303 them to the
    same page. Keeps platform admin actions auditable to the operator.
    """
    qs = []
    if notice:
        qs.append(f"notice={quote(notice)}")
    if error:
        qs.append(f"error={quote(error)}")
    tail = "?" + "&".join(qs) if qs else ""
    return RedirectResponse(url=f"/platform/admin/tenants{tail}", status_code=303)


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
    notice: str | None = None,
    error: str | None = None,
    identity: Identity = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_platform_db),
) -> HTMLResponse:
    tenants = await list_tenants(db)
    # Fetch the signed-in platform admin's memberships so the template
    # can show "you already have support access" per tenant row.
    own_memberships = (
        (
            await db.execute(
                select(TenantMembership).where(TenantMembership.identity_id == identity.id)
            )
        )
        .scalars()
        .all()
    )
    access_by_tenant_id: dict = {
        str(m.tenant_id): m.access_type for m in own_memberships if m.is_active
    }
    html = _templates(request).render(
        request,
        "platform/admin/tenants.html",
        {
            "identity": identity,
            "tenants": tenants,
            "access_by_tenant_id": access_by_tenant_id,
            "error": error,
            "notice": notice,
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
            # Platform admin is a trusted provisioning path; the
            # identity never receives a verification email and must
            # not be trapped behind the verify gate on first login.
            # Round-3 audit Backend P2.
            pre_verified_identity=True,
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
    return _redir_tenants(notice=f"Tenant „{slug}“ vytvořen.")


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

    # Real MRR = sum of monthly plan prices for currently-active
    # (including trialing and demo) subscriptions, grouped by
    # currency so we never sum across CZK / EUR. For simplicity we
    # take the dominant currency (first row) and report it; a
    # multi-currency deployment would break this out per currency.
    mrr_rows = (
        await db.execute(
            select(
                Plan.currency,
                func.coalesce(func.sum(Plan.monthly_price_cents), 0).label("total"),
            )
            .join(Subscription, Subscription.plan_id == Plan.id)
            .where(Subscription.status.in_(("active", "trialing", "demo")))
            .group_by(Plan.currency)
            .order_by(func.sum(Plan.monthly_price_cents).desc())
        )
    ).all()
    mrr_cents = int(mrr_rows[0].total) if mrr_rows else 0
    mrr_currency = mrr_rows[0].currency if mrr_rows else "CZK"

    # Keep the 30-day *paid invoice* figure around too — useful for
    # rough validation once live Stripe webhooks start writing rows.
    paid_30d_cents = int(
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
                "mrr_currency": mrr_currency,
                "paid_30d_cents": paid_30d_cents,
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
    return _redir_tenants(notice="Tenant deaktivován.")


@router.post("/tenants/{tenant_id}/reactivate")
async def tenants_reactivate(
    tenant_id: UUID,
    identity: Identity = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_platform_db),
) -> Response:
    try:
        await reactivate_tenant(db, tenant_id=tenant_id)
    except PlatformError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    await db.commit()
    return _redir_tenants(notice="Tenant reaktivován.")


@router.get("/tenants/{tenant_id}/edit", response_class=HTMLResponse)
async def tenants_edit_form(
    tenant_id: UUID,
    request: Request,
    identity: Identity = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_platform_db),
) -> HTMLResponse:
    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404)
    html = _templates(request).render(
        request,
        "platform/admin/tenant_edit.html",
        {
            "identity": identity,
            "tenant": tenant,
            "error": None,
            "principal": None,
        },
    )
    return HTMLResponse(html)


@router.post("/tenants/{tenant_id}/edit", response_class=HTMLResponse)
async def tenants_edit(
    tenant_id: UUID,
    request: Request,
    name: str = Form(...),
    billing_email: str = Form(...),
    identity: Identity = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_platform_db),
) -> Response:
    try:
        await update_tenant(
            db,
            tenant_id=tenant_id,
            name=name,
            billing_email=billing_email,
        )
    except PlatformError as exc:
        tenant = (
            await db.execute(select(Tenant).where(Tenant.id == tenant_id))
        ).scalar_one_or_none()
        html = _templates(request).render(
            request,
            "platform/admin/tenant_edit.html",
            {
                "identity": identity,
                "tenant": tenant,
                "error": str(exc),
                "principal": None,
            },
        )
        return HTMLResponse(html, status_code=400)
    await db.commit()
    return _redir_tenants(notice="Změny uloženy.")


@router.post("/tenants/{tenant_id}/support-access")
async def tenants_grant_support_access(
    tenant_id: UUID,
    request: Request,
    identity: Identity = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_platform_db),
) -> Response:
    """Enrol the platform admin as a tenant_admin User + TenantMembership
    with ``access_type=support`` so they can enter the tenant via the
    normal /platform/select-tenant switch flow. This is an explicit
    opt-in, auditable step — no silent impersonation anywhere.
    """
    from sqlalchemy import text

    from app.services import audit_service

    try:
        user, _ = await grant_platform_admin_support_access(
            db,
            identity=identity,
            tenant_id=tenant_id,
        )
    except PlatformError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    # Audit to the target tenant's log — that's where the tenant admin
    # looks to see who entered their portal.
    await db.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": str(tenant_id)},
    )
    await audit_service.record(
        db,
        action="platform.support_access_granted",
        entity_type="user",
        entity_id=user.id,
        entity_label=identity.email,
        actor=audit_service.ActorInfo(
            type="system",
            id=None,
            label=f"platform-admin:{identity.email}",
        ),
        tenant_id=tenant_id,
    )
    await db.commit()
    return _redir_tenants(notice="Support přístup přidělen.")


@router.post("/tenants/{tenant_id}/revoke-support")
async def tenants_revoke_support_access(
    tenant_id: UUID,
    request: Request,
    identity: Identity = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_platform_db),
) -> Response:
    """Drop the platform admin's ``access_type=support`` membership + the
    matching User's active flag. Records a ``platform.support_access_revoked``
    audit event so the tenant sees the full grant → revoke trail.
    """
    from sqlalchemy import text

    from app.services import audit_service

    result = await revoke_platform_admin_support_access(
        db,
        identity=identity,
        tenant_id=tenant_id,
    )
    if result is None:
        # Nothing to revoke — treat as no-op so double-click from the
        # UI doesn't 500. The tenants page will show the correct state.
        return _redir_tenants(notice="Žádný support přístup k zrušení.")

    user, _ = result
    await db.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": str(tenant_id)},
    )
    await audit_service.record(
        db,
        action="platform.support_access_revoked",
        entity_type="user",
        entity_id=user.id,
        entity_label=identity.email,
        actor=audit_service.ActorInfo(
            type="system",
            id=None,
            label=f"platform-admin:{identity.email}",
        ),
        tenant_id=tenant_id,
    )
    await db.commit()
    return _redir_tenants(notice="Support přístup zrušen.")
