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

    # Subscription + plan per tenant — single round-trip via JOIN, then
    # bucket by tenant_id for O(1) template lookup. Self-host /
    # pre-billing tenants have no row in platform_subscriptions; they
    # render as "—" in the Plan column.
    sub_rows = (
        await db.execute(
            select(Subscription, Plan)
            .join(Plan, Plan.id == Subscription.plan_id)
            .order_by(Subscription.tenant_id)
        )
    ).all()
    sub_by_tenant_id: dict = {
        str(sub.tenant_id): {"sub": sub, "plan": plan} for sub, plan in sub_rows
    }

    html = _templates(request).render(
        request,
        "platform/admin/tenants.html",
        {
            "identity": identity,
            "tenants": tenants,
            "access_by_tenant_id": access_by_tenant_id,
            "sub_by_tenant_id": sub_by_tenant_id,
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
    # Cancel the active Stripe subscription before pulling the rug.
    # An operator-driven deactivation is functionally a hard cancel —
    # without this, Stripe keeps billing the customer for a service
    # they no longer have access to. Idempotent: cancel_subscription
    # is a no-op when the sub is already canceled or absent.
    from app.config import get_settings as _get_settings
    from app.platform.billing.service import (
        BillingError,
        cancel_subscription,
        get_subscription_for_tenant,
    )

    settings = _get_settings()
    sub = await get_subscription_for_tenant(db, tenant_id)
    if sub is not None and sub.status in {"active", "trialing", "past_due"}:
        try:
            await cancel_subscription(
                db,
                settings,
                subscription=sub,
                actor_label=f"platform-admin/{identity.email}",
            )
        except BillingError:
            # Stripe failure shouldn't block the deactivation — the
            # operator already decided this tenant is going away. Log
            # and proceed; an orphan Stripe sub can be cleaned up
            # manually from the Stripe dashboard.
            from app.logging import get_logger

            get_logger("app.platform").warning(
                "tenant_deactivate.stripe_cancel_failed",
                tenant_id=str(tenant_id),
            )

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


# --------------------------------------------------- subscription editor


@router.get("/tenants/{tenant_id}/subscription", response_class=HTMLResponse)
async def subscription_edit_form(
    tenant_id: UUID,
    request: Request,
    notice: str | None = None,
    error: str | None = None,
    identity: Identity = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_platform_db),
) -> HTMLResponse:
    """Show the subscription editor for one tenant.

    Three states the form copes with:
    * No subscription row at all (self-host / pre-billing signup) →
      offer a "Start trial" button.
    * Subscription with no Stripe id (demo mode, manual extension) →
      full edit: plan + trial_ends_at + current_period_end + quick
      actions (extend +30/+90/+365, pin to 2099 for internal tenants).
    * Subscription managed by Stripe → readonly view + warning that
      changes must be made in the Stripe dashboard, otherwise the next
      webhook will overwrite local state.
    """
    from app.platform.billing.service import get_subscription_for_tenant, list_plans

    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404)
    sub = await get_subscription_for_tenant(db, tenant_id)
    plans = await list_plans(db)
    current_plan = None
    if sub is not None:
        current_plan = next((p for p in plans if p.id == sub.plan_id), None)

    html = _templates(request).render(
        request,
        "platform/admin/subscription_edit.html",
        {
            "identity": identity,
            "tenant": tenant,
            "subscription": sub,
            "current_plan": current_plan,
            "plans": plans,
            "stripe_managed": bool(sub and sub.stripe_subscription_id),
            "error": error,
            "notice": notice,
            "principal": None,
        },
    )
    return HTMLResponse(html)


@router.post("/tenants/{tenant_id}/subscription", response_class=HTMLResponse)
async def subscription_edit(
    tenant_id: UUID,
    request: Request,
    plan_code: str = Form(""),
    trial_ends_at: str = Form(""),
    current_period_end: str = Form(""),
    quick_action: str = Form(""),
    identity: Identity = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_platform_db),
) -> Response:
    """Apply edits to a tenant's subscription. Refuses on Stripe-managed
    subscriptions. Quick actions take precedence over date fields when
    set. All changes audited under the target tenant."""
    from datetime import UTC, datetime, timedelta
    from datetime import date as _date

    from app.platform.billing.service import (
        get_subscription_for_tenant,
        require_plan,
        set_subscription_plan,
        start_trial_subscription,
    )
    from app.services import audit_service
    from app.services.audit_service import ActorInfo

    redir = f"/platform/admin/tenants/{tenant_id}/subscription"

    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404)

    sub = await get_subscription_for_tenant(db, tenant_id)

    # No subscription → quick_action="start_trial" mints one (Starter,
    # 30 days). Any other action is a no-op on a missing row.
    if sub is None:
        if quick_action == "start_trial":
            try:
                sub = await start_trial_subscription(db, tenant=tenant, plan_code="starter")
            except Exception as exc:
                return RedirectResponse(url=f"{redir}?error={quote(str(exc))}", status_code=303)
            await db.commit()
            return RedirectResponse(
                url=f"{redir}?notice={quote('Trial spuštěn (Starter, 30 dní).')}",
                status_code=303,
            )
        return RedirectResponse(
            url=f"{redir}?error={quote('Tenant nemá subscription. Klikni Start trial.')}",
            status_code=303,
        )

    # Stripe-managed → refuse manual edit; the next webhook would
    # overwrite anything we set anyway.
    if sub.stripe_subscription_id:
        return RedirectResponse(
            url=(
                f"{redir}?error="
                + quote(
                    "Předplatné spravuje Stripe. Změny dělej ve Stripe dashboardu — "
                    "uložení tady přepíše příští webhook."
                )
            ),
            status_code=303,
        )

    before = {
        "plan_id": str(sub.plan_id),
        "status": sub.status,
        "trial_ends_at": sub.trial_ends_at.isoformat() if sub.trial_ends_at else None,
        "current_period_end": (
            sub.current_period_end.isoformat() if sub.current_period_end else None
        ),
    }

    # Quick actions short-circuit the date fields (deliberate: they're
    # the safer path for the common case).
    now = datetime.now(UTC)
    if quick_action.startswith("extend_trial:"):
        days = int(quick_action.split(":", 1)[1])
        anchor = sub.trial_ends_at or now
        if anchor < now:
            anchor = now
        sub.trial_ends_at = anchor + timedelta(days=days)
        sub.current_period_end = sub.trial_ends_at
        sub.status = "trialing"
    elif quick_action == "pin_internal":
        # Internal-team tenants (e.g. operator's own portal) shouldn't
        # ever auto-expire. 2099-01-01 is far enough out that we'll
        # have replaced this code by then.
        far_future = datetime(2099, 1, 1, tzinfo=UTC)
        sub.trial_ends_at = far_future
        sub.current_period_end = far_future
        sub.status = "trialing"
    elif quick_action == "set_active":
        # Operator decided to mark it as active without going through
        # Stripe (e.g. handshake-billed enterprise tenant). Drops
        # trial_ends_at, keeps current_period_end as the next renewal.
        sub.status = "active"
        sub.trial_ends_at = None
        if not sub.current_period_end or sub.current_period_end < now:
            sub.current_period_end = now + timedelta(days=30)
    else:
        # Free-form edit. Plan first, then dates.
        if plan_code:
            try:
                plan = await require_plan(db, plan_code)
                await set_subscription_plan(db, subscription=sub, plan=plan)
            except Exception as exc:
                return RedirectResponse(
                    url=f"{redir}?error={quote(f'Plan: {exc}')}", status_code=303
                )

        def _parse(s: str) -> datetime | None:
            s = (s or "").strip()
            if not s:
                return None
            try:
                d = _date.fromisoformat(s)
            except ValueError:
                return None
            return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=UTC)

        new_trial = _parse(trial_ends_at)
        new_period = _parse(current_period_end)
        if new_trial is not None or trial_ends_at == "":
            sub.trial_ends_at = new_trial
        if new_period is not None or current_period_end == "":
            sub.current_period_end = new_period
        # Keep status sane after manual date juggling: if a future
        # trial_ends_at is set and status was canceled/past_due, flip
        # back to trialing. Operator can override by also passing
        # quick_action=set_active.
        if (
            sub.trial_ends_at
            and sub.trial_ends_at > now
            and sub.status
            in {
                "canceled",
                "past_due",
            }
        ):
            sub.status = "trialing"

    await db.flush()

    after = {
        "plan_id": str(sub.plan_id),
        "status": sub.status,
        "trial_ends_at": sub.trial_ends_at.isoformat() if sub.trial_ends_at else None,
        "current_period_end": (
            sub.current_period_end.isoformat() if sub.current_period_end else None
        ),
    }
    if before != after:
        await audit_service.record(
            db,
            action="billing.subscription_edited_by_platform_admin",
            entity_type="subscription",
            entity_id=sub.id,
            entity_label=f"plan={sub.plan_id} status={sub.status}",
            actor=ActorInfo(type="user", id=None, label=f"platform-admin/{identity.email}"),
            before=before,
            after=after,
            tenant_id=tenant.id,
        )
    await db.commit()
    return RedirectResponse(
        url=f"{redir}?notice={quote('Změny uloženy.')}",
        status_code=303,
    )


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
