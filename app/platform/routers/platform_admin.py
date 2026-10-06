"""Platform admin: CRUD tenants, basic oversight.

Gated by `require_platform_admin`, so a regular Identity cannot see it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.i18n import t as _t
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
    from app.config import get_settings
    from app.platform.billing.early_access import covered_by_early_access, effective_trial_end

    settings = get_settings()
    sub_by_tenant_id: dict = {
        str(sub.tenant_id): {
            "sub": sub,
            "plan": plan,
            # E1: the date the trial really ends (early access included).
            "trial_until": effective_trial_end(sub, settings),
            "early_access": covered_by_early_access(sub, settings),
        }
        for sub, plan in sub_rows
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
                "error": _t(request, "A tenant with slug '%(slug)s' already exists.")
                % {"slug": slug},
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
    return _redir_tenants(notice=_t(request, "Tenant '%(slug)s' created.") % {"slug": slug})


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

    # BIZ-09: "active subscriptions" and MRR count only subscriptions
    # somebody is actually paying for — Stripe-managed ``active`` ones,
    # plus manually invoiced ``active`` rows the operator marked with an
    # end date (status_changed_at is set by the subscription editor).
    # Trials and demo rows used to be summed into MRR, which showed
    # 5 880 Kč of revenue against zero invoices.
    paid_filter = (Subscription.status == "active") & (
        Subscription.stripe_subscription_id.is_not(None)
        | Subscription.status_changed_at.is_not(None)
    )
    subs_active = int(
        (await db.execute(select(func.count(Subscription.id)).where(paid_filter))).scalar_one()
    )
    subs_trialing = int(
        (
            await db.execute(
                select(func.count(Subscription.id)).where(
                    Subscription.status.in_(("trialing", "demo"))
                )
            )
        ).scalar_one()
    )
    # Trials whose owner actually confirmed the e-mail address — the
    # bot signups never do, so this is the trial number worth watching.
    from app.models.enums import UserRole
    from app.models.user import User

    subs_trialing_verified = int(
        (
            await db.execute(
                select(func.count(func.distinct(Subscription.id)))
                .join(TenantMembership, TenantMembership.tenant_id == Subscription.tenant_id)
                .join(User, User.id == TenantMembership.user_id)
                .join(Identity, Identity.id == TenantMembership.identity_id)
                .where(
                    Subscription.status.in_(("trialing", "demo")),
                    User.role == UserRole.TENANT_ADMIN,
                    TenantMembership.access_type != "support",
                    Identity.email_verified_at.is_not(None),
                )
            )
        ).scalar_one()
    )

    # MRR = sum of monthly plan prices of PAID subscriptions, grouped by
    # currency so we never sum across CZK / EUR. For simplicity we take
    # the dominant currency (first row) and report it; a multi-currency
    # deployment would break this out per currency.
    mrr_rows = (
        await db.execute(
            select(
                Plan.currency,
                func.coalesce(func.sum(Plan.monthly_price_cents), 0).label("total"),
            )
            .join(Subscription, Subscription.plan_id == Plan.id)
            .where(paid_filter)
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

    # E1: how many trials currently end on the early-access date (SQL
    # twin of app.services.early_access.covered_by_early_access_for).
    from app.config import get_settings
    from app.services.early_access import (
        COVERED_BY_EARLY_ACCESS_SQL,
        early_access_ends_at,
        early_access_info,
    )

    settings = get_settings()
    early_access = early_access_info(
        settings.early_access_until, getattr(request.state, "locale", None), now
    )
    early_access_tenants = 0
    if early_access.ends_at is not None:
        early_access_tenants = int(
            (
                await db.execute(
                    text(
                        "SELECT count(*) FROM platform_subscriptions s "
                        f"WHERE {COVERED_BY_EARLY_ACCESS_SQL}"
                    ),
                    {"early_access_end": early_access_ends_at(settings.early_access_until)},
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
                "subs_trialing_verified": subs_trialing_verified,
                "mrr_cents": mrr_cents,
                "mrr_currency": mrr_currency,
                "paid_30d_cents": paid_30d_cents,
            },
            "recent_signups": recent_signups,
            "early_access": early_access,
            "early_access_tenants": early_access_tenants,
            "principal": None,
        },
    )
    return HTMLResponse(html)


@router.get("/funnel", response_class=HTMLResponse)
async def admin_funnel(
    request: Request,
    identity: Identity = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_platform_db),
) -> HTMLResponse:
    """Activation funnel per signup week + per-tenant activation columns.

    Verified signups → portal created → first customer invited → first
    customer login → first order placed by a customer. Unverified
    identities (bots, typos) are shown as an excluded count, never in the
    denominator (BIZ-09, BIZ-16).
    """
    from app.platform.activation import (
        FUNNEL_STAGES,
        funnel_totals,
        signup_refs,
        tenant_activation,
        weekly_funnel,
    )

    weeks = await weekly_funnel(db, weeks=12)
    html = _templates(request).render(
        request,
        "platform/admin/funnel.html",
        {
            "identity": identity,
            "weeks": weeks,
            "totals": funnel_totals(weeks),
            "stages": FUNNEL_STAGES,
            "tenants": await tenant_activation(db, limit=50),
            "refs": await signup_refs(db, days=90),
            "principal": None,
        },
    )
    return HTMLResponse(html)


@router.post("/tenants/{tenant_id}/deactivate")
async def tenants_deactivate(
    tenant_id: UUID,
    request: Request,
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
    if sub is not None:
        # Codex-6: record that THIS deactivation is an operator decision.
        # A later Stripe "active" update (e.g. the cancel-at-period-end
        # toggle below) may undo a billing cut, never this.
        sub.operator_suspended_at = sub.operator_suspended_at or datetime.now(UTC)
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
    return _redir_tenants(notice=_t(request, "Tenant deactivated."))


@router.post("/tenants/{tenant_id}/reactivate")
async def tenants_reactivate(
    tenant_id: UUID,
    request: Request,
    identity: Identity = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_platform_db),
) -> Response:
    from app.platform.billing.service import get_subscription_for_tenant

    try:
        await reactivate_tenant(db, tenant_id=tenant_id)
    except PlatformError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    notice = _t(request, "Tenant reactivated.")
    sub = await get_subscription_for_tenant(db, tenant_id)
    if sub is not None:
        sub.operator_suspended_at = None
        if sub.status not in {"active", "trialing", "demo"}:
            # Without this hint the next daily billing job silently
            # deactivates the tenant again.
            notice += " " + _t(
                request,
                "Note: the subscription is “{status}” — unless you extend it in the "
                "subscription editor, the daily billing job will deactivate the tenant again.",
            ).format(status=sub.status)
    await db.commit()
    return _redir_tenants(notice=notice)


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
    return _redir_tenants(notice=_t(request, "Changes saved."))


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

    from app.config import get_settings
    from app.platform.billing.early_access import covered_by_early_access, effective_trial_end

    settings = get_settings()
    html = _templates(request).render(
        request,
        "platform/admin/subscription_edit.html",
        {
            "identity": identity,
            "tenant": tenant,
            "subscription": sub,
            "trial_until": effective_trial_end(sub, settings),
            "early_access": covered_by_early_access(sub, settings),
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
    active_until: str = Form(""),
    quick_action: str = Form(""),
    identity: Identity = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_platform_db),
) -> Response:
    """Apply edits to a tenant's subscription. Refuses on Stripe-managed
    subscriptions. Quick actions take precedence over date fields when
    set. All changes audited under the target tenant.

    2026-10-03 audit (LOGIC-18 / SEC-11):

    * ``quick_action`` is validated — a malformed value is an error
      flash, not a 500;
    * "set active" (manual billing) requires an explicit end date
      (``active_until``); the periodic expiry job cancels such a row at
      that date like a trial, instead of leaving it free forever;
    * actions that grant time also lift a *billing* hard cut
      (``tenants.is_active``), so "extend trial" on a lapsed tenant
      really lets them back in. An operator suspension is never lifted
      here — that is the explicit Reactivate button.
    """
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

    def _error(msg: str) -> RedirectResponse:
        return RedirectResponse(url=f"{redir}?error={quote(msg)}", status_code=303)

    def _parse(s: str) -> datetime | None:
        s = (s or "").strip()
        if not s:
            return None
        try:
            d = _date.fromisoformat(s)
        except ValueError:
            return None
        return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=UTC)

    # Validate the quick action up front.
    extend_days: int | None = None
    if quick_action.startswith("extend_trial:"):
        try:
            extend_days = int(quick_action.split(":", 1)[1])
        except ValueError:
            extend_days = None
        if extend_days is None or not 1 <= extend_days <= 3650:
            return _error(_t(request, "Invalid action: the number of days must be 1 to 3650."))
    elif quick_action not in {"", "start_trial", "pin_internal", "set_active"}:
        return _error(_t(request, "Unknown action."))

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
                return _error(str(exc))
            await db.commit()
            return RedirectResponse(
                url=f"{redir}?notice={quote(_t(request, 'Trial started (Starter, 30 days).'))}",
                status_code=303,
            )
        return _error(_t(request, "Tenant has no subscription. Click Start trial."))

    # Stripe-managed → refuse manual edit; the next webhook would
    # overwrite anything we set anyway.
    if sub.stripe_subscription_id:
        return _error(
            _t(
                request,
                "The subscription is managed by Stripe. Make changes in the "
                "Stripe dashboard — saving here would be overwritten by the "
                "next webhook.",
            )
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
    grants_time = False
    if extend_days is not None:
        # Extend from the date the trial really ends — early access (E1)
        # included — so "+30 days" never lands before what they have.
        from app.config import get_settings
        from app.platform.billing.early_access import effective_trial_end

        anchor = effective_trial_end(sub, get_settings()) or now
        if anchor < now:
            anchor = now
        sub.trial_ends_at = anchor + timedelta(days=extend_days)
        sub.current_period_end = sub.trial_ends_at
        sub.status = "trialing"
        grants_time = True
    elif quick_action == "pin_internal":
        # Internal-team tenants (e.g. operator's own portal) shouldn't
        # ever auto-expire. 2099-01-01 is far enough out that we'll
        # have replaced this code by then.
        far_future = datetime(2099, 1, 1, tzinfo=UTC)
        sub.trial_ends_at = far_future
        sub.current_period_end = far_future
        sub.status = "trialing"
        grants_time = True
    elif quick_action == "set_active":
        # Operator marks it active without Stripe (invoice / bank
        # transfer). The paid-until date is mandatory: the expiry job
        # cancels the row at that date, exactly like a trial.
        until = _parse(active_until)
        if until is None or until <= now:
            return _error(_t(request, "“Mark as active” needs a “paid until” date in the future."))
        sub.status = "active"
        sub.trial_ends_at = None
        sub.current_period_end = until
        # Marks the row as an editor-managed manual subscription (also
        # when it was already 'active' — the ORM listener only stamps
        # real status changes). Legacy rows without it never expire.
        sub.status_changed_at = now
        grants_time = True
    elif quick_action == "start_trial":
        return _error(_t(request, "The tenant already has a subscription."))
    else:
        # Free-form edit. Plan first, then dates.
        if plan_code:
            try:
                plan = await require_plan(db, plan_code)
                await set_subscription_plan(db, subscription=sub, plan=plan)
            except Exception as exc:
                return _error(f"Plan: {exc}")

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
        if sub.trial_ends_at and sub.trial_ends_at > now and sub.status in {"canceled", "past_due"}:
            sub.status = "trialing"
            grants_time = True

    notice = _t(request, "Changes saved.")
    if grants_time:
        sub.canceled_at = None
        sub.cancel_at_period_end = False
        if not tenant.is_active:
            if sub.operator_suspended_at is None:
                tenant.is_active = True
                notice += " " + _t(request, "The tenant was reactivated.")
            else:
                notice += " " + _t(
                    request,
                    "The tenant stays suspended by the operator — reactivate it in the "
                    "tenant list if intended.",
                )

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
        url=f"{redir}?notice={quote(notice)}",
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
    return _redir_tenants(notice=_t(request, "Support access granted."))


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

    from app.services import audit_service

    result = await revoke_platform_admin_support_access(
        db,
        identity=identity,
        tenant_id=tenant_id,
    )
    if result is None:
        # Nothing to revoke — treat as no-op so double-click from the
        # UI doesn't 500. The tenants page will show the correct state.
        return _redir_tenants(notice=_t(request, "No support access to revoke."))

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
    return _redir_tenants(notice=_t(request, "Support access revoked."))
