"""Billing routes: subscription dashboard, checkout, webhooks.

All routes require an authenticated platform Identity (``require_identity``).
The dashboard shows the tenant's current plan, trial status, usage snapshot,
and invoice history; checkout kicks off a Stripe Checkout session (or a
plan change on the existing Stripe subscription), or — without Stripe —
either the local demo switch (dev/staging) or the "we invoice by bank
transfer" message (production, CEO decision D2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote, urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.i18n import t as _t
from app.logging import get_logger
from app.models.tenant import Tenant
from app.models.user import User
from app.platform.billing.service import (
    BLOCKED_STRIPE_STATUSES,
    HIDDEN_PLAN_CODES,
    NON_CHECKOUT_PLAN_CODES,
    BillingError,
    CheckoutInProgress,
    cancel_subscription,
    change_subscription_plan,
    create_billing_portal_session,
    get_plan_by_code,
    get_subscription_for_tenant,
    list_invoices_for_tenant,
    list_plans,
    open_checkout_session,
    set_subscription_plan,
    verify_webhook,
)
from app.platform.deps import get_platform_db, require_verified_identity
from app.platform.models import Identity
from app.platform.service import list_memberships_for_identity
from app.security.csrf import verify_csrf

log = get_logger("app.billing")

router = APIRouter(tags=["platform-billing"])

# GET routes don't need CSRF; the checkout POST + portal POST do.
csrf_router = APIRouter(tags=["platform-billing"], dependencies=[Depends(verify_csrf)])

# Advisory-lock namespace (two-int form: namespace, hashtext(tenant_id))
# serialising every money-moving billing action of one tenant — checkout
# creation, plan change, cancellation. Distinct from the single-bigint
# job locks (42_001…) so the key spaces cannot collide.
BILLING_LOCK_NAMESPACE = 42_101

# Statuses whose Stripe subscription is over; a new purchase goes
# through a fresh Checkout.
_ENDED_STRIPE_STATUSES = frozenset({"canceled", "incomplete_expired"})

# A trial / lapsed tenant without a Stripe subscription gets a primary
# "Continue on <plan>" button on its current plan (BIZ-02).
_CONTINUE_STATUSES = frozenset({"trialing", "canceled", "demo"})


def _templates(request: Request):
    return request.app.state.templates


# Keys we expect the operator to have filled before any Stripe checkout
# can run. ``billing_ico`` is the Czech company ID — without it the
# generated tax doklad is invalid. The other two are needed for the
# fakturační adresa block. ``billing_dic`` is optional (a non-VAT-payer
# can sell without one).
REQUIRED_BILLING_KEYS: tuple[str, ...] = ("billing_ico", "billing_name", "billing_address")


def _billing_details_present(tenant: Tenant) -> bool:
    blob = tenant.settings or {}
    return all(str(blob.get(k) or "").strip() for k in REQUIRED_BILLING_KEYS)


# ------------------------------------------------------- billing authority


@dataclass
class BillingContext:
    """The tenant an identity is billing right now, plus its other choices."""

    tenant: Tenant
    user: User
    choices: list[Tenant] = field(default_factory=list)
    # Plain copies taken up front: a ``rollback()`` on an error path
    # expires the ORM rows, and touching them afterwards would need IO.
    tenant_id: UUID = field(init=False)
    slug: str = field(init=False)
    user_id: UUID = field(init=False)
    user_session_version: int = field(init=False)
    multi: bool = field(init=False)

    def __post_init__(self) -> None:
        self.tenant_id = self.tenant.id
        self.slug = self.tenant.slug
        self.user_id = self.user.id
        self.user_session_version = self.user.session_version
        self.multi = len(self.choices) > 1

    @property
    def tenant_qs(self) -> str:
        """``?tenant=<slug>`` when the identity bills more than one tenant.

        Single-tenant owners (almost everyone) keep clean URLs; owners of
        several tenants carry the choice through every form and redirect
        (LOGIC-23).
        """
        if self.multi:
            return "?" + urlencode({"tenant": self.slug})
        return ""

    def url(self, path: str = "/platform/billing", **params: str) -> str:
        query = {k: v for k, v in params.items() if v is not None}
        if self.multi:
            query["tenant"] = self.slug
        return f"{path}?{urlencode(query)}" if query else path


async def _billing_targets(db: AsyncSession, identity: Identity) -> list[tuple[Tenant, User]]:
    """Every tenant this identity may bill, in membership order.

    Billing is a per-tenant concern AND a privileged one: cancelling a
    subscription or switching plans has real money consequences. We
    therefore require an **active** ``UserRole.TENANT_ADMIN`` user
    behind the membership:

    * customer-contact memberships are skipped (customers never see
      their supplier's billing);
    * plain tenant_staff memberships are skipped;
    * **support-access memberships are skipped** — a platform operator's
      support grant is a TENANT_ADMIN user in the customer's tenant, but
      support access is for looking at business data through an audited
      grant and is never authority over that tenant's money;
    * **disabled admins are skipped** (Codex-2) — disabling an
      administrator in the tenant invalidated their tenant session but
      left their platform membership, so they could still cancel the
      subscription or read invoices from the apex.
    """
    from app.models.enums import UserRole
    from app.platform.models import MEMBERSHIP_ACCESS_SUPPORT
    from app.platform.service import resolve_membership_targets

    out: list[tuple[Tenant, User]] = []
    memberships = await list_memberships_for_identity(db, identity_id=identity.id)
    for membership in memberships:
        if membership.user_id is None:
            continue  # customer contacts don't manage billing
        if membership.access_type == MEMBERSHIP_ACCESS_SUPPORT:
            continue  # operator support grant — never a billing identity
        tenant, target = await resolve_membership_targets(db, membership=membership)
        if tenant is None or not isinstance(target, User):
            continue
        if target.role != UserRole.TENANT_ADMIN or not target.is_active:
            continue
        out.append((tenant, target))
    return out


async def _resolve_current_tenant(
    db: AsyncSession,
    identity: Identity,
    request: Request | None = None,
    tenant_slug: str | None = None,
) -> tuple[Tenant, User] | tuple[None, None]:
    """Pick the tenant to bill (see :func:`_resolve_billing_context`)."""
    ctx = await _billing_context_or_none(db, identity, request, tenant_slug)
    if ctx is None:
        return None, None
    return ctx.tenant, ctx.user


async def _billing_context_or_none(
    db: AsyncSession,
    identity: Identity,
    request: Request | None,
    tenant_slug: str | None,
) -> BillingContext | None:
    targets = await _billing_targets(db, identity)
    if not targets:
        return None
    choices = [t for t, _ in targets]

    # 1. Explicit ``?tenant=<slug>`` is strict: an identity asking for a
    #    tenant it cannot bill gets nothing, not silently another tenant.
    if tenant_slug:
        wanted = tenant_slug.strip().lower()
        for t, u in targets:
            if t.slug == wanted:
                return BillingContext(t, u, choices)
        return None

    # 2. The tenant subdomain the request came from (the in-app
    #    "Billing" link is relative, so it lands on {slug}.apex) is a
    #    soft hint — fall through when it is not one of ours.
    if request is not None:
        from app.deps import resolve_tenant_slug

        hint = resolve_tenant_slug(request, get_settings())
        if hint:
            for t, u in targets:
                if t.slug == hint:
                    return BillingContext(t, u, choices)

    # 3. Deterministic first membership.
    t, u = targets[0]
    return BillingContext(t, u, choices)


async def _resolve_billing_context(
    db: AsyncSession,
    identity: Identity,
    request: Request,
    tenant_slug: str | None,
) -> BillingContext:
    ctx = await _billing_context_or_none(db, identity, request, tenant_slug)
    if ctx is None:
        raise HTTPException(status_code=404, detail="No tenant to manage")
    return ctx


async def _lock_tenant_billing(db: AsyncSession, tenant_id: UUID) -> None:
    """Serialise billing actions of one tenant until the transaction ends.

    Two quick clicks (or two tabs) on "Upgrade" used to race into two
    Checkout sessions — two subscriptions once both were paid (Codex-1).
    """
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:ns, hashtext(:tid))"),
        {"ns": BILLING_LOCK_NAMESPACE, "tid": str(tenant_id)},
    )


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


# --------------------------------------------------------- billing dashboard


@router.get("/platform/billing/invoices/{invoice_id}.pdf")
async def invoice_pdf(
    invoice_id: UUID,
    request: Request,
    tenant: str | None = None,
    identity: Identity = Depends(require_verified_identity),
    db: AsyncSession = Depends(get_platform_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Download a CZ-tax-compliant PDF for a single invoice.

    Scoped to the caller's tenant — an identity cannot pull another
    tenant's invoice even if they know the UUID. Builds the PDF
    on-the-fly rather than storing it; the source of truth stays in
    Stripe, we just render a locally-compliant doklad each time.
    """
    from sqlalchemy import select

    from app.platform.billing.models import Invoice
    from app.services.invoice_pdf_service import _safe_filename_for, render_invoice_pdf

    ctx = await _resolve_billing_context(db, identity, request, tenant)

    invoice = (
        await db.execute(
            select(Invoice).where(Invoice.id == invoice_id, Invoice.tenant_id == ctx.tenant.id)
        )
    ).scalar_one_or_none()
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # Re-fetch tenant as the ORM row so its ``settings`` JSONB is accessible.
    tenant_row = (await db.execute(select(Tenant).where(Tenant.id == ctx.tenant.id))).scalar_one()

    # Locale priority: caller's current UI choice (so a German contact
    # logged in to /platform sees DE on download) > tenant business
    # default > CS. Falls through to CS for any locale we don't ship a
    # label set for.
    request_locale = getattr(request.state, "locale", None)
    pdf_bytes = render_invoice_pdf(
        invoice=invoice,
        tenant=tenant_row,
        settings=settings,
        locale=request_locale,
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{_safe_filename_for(invoice)}"'},
    )


@router.get("/platform/billing", response_class=HTMLResponse)
async def billing_dashboard(
    request: Request,
    checkout: str | None = None,
    checkout_plan: str | None = None,
    plan: str | None = None,
    notice: str | None = None,
    error: str | None = None,
    tenant: str | None = None,
    identity: Identity = Depends(require_verified_identity),
    db: AsyncSession = Depends(get_platform_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    ctx = await _resolve_billing_context(db, identity, request, tenant)

    subscription = await get_subscription_for_tenant(db, ctx.tenant.id)
    plans = await list_plans(db)
    invoices = await list_invoices_for_tenant(db, ctx.tenant.id)

    current_plan = None
    if subscription is not None:
        for p in plans:
            if p.id == subscription.plan_id:
                current_plan = p
                break

    # Pass a usage snapshot so the dashboard can render progress bars
    # against the current plan caps.
    from app.platform.usage import snapshot_tenant_usage

    usage = await snapshot_tenant_usage(db, ctx.tenant.id)

    if checkout == "success":
        if settings.stripe_enabled:
            notice = _t(
                request,
                "Thank you! Your subscription will show here as soon as Stripe confirms "
                "the payment — usually within a minute.",
            )
        else:
            notice = _t(request, "Subscription updated.")
    # ``notice`` may also arrive as a query-string flash from the
    # cancel-subscription route; in that case keep what was sent.

    live_stripe_sub = bool(
        subscription is not None
        and subscription.stripe_subscription_id
        and subscription.status not in _ENDED_STRIPE_STATUSES
    )
    # BIZ-02: a trialist (or a lapsed tenant) without a Stripe
    # subscription needs a primary button to keep the plan they are on —
    # the dashboard used to show only the words "Current plan".
    continue_plan = None
    if (
        subscription is not None
        and current_plan is not None
        and not live_stripe_sub
        and subscription.status in _CONTINUE_STATUSES
        and current_plan.code not in NON_CHECKOUT_PLAN_CODES
    ):
        continue_plan = current_plan

    offline_plan = None
    if checkout == "offline" and plan:
        offline_plan = next((p for p in plans if p.code == plan), None)
    resume_plan = None
    if checkout_plan:
        resume_plan = next(
            (p for p in plans if p.code == checkout_plan and p.code not in NON_CHECKOUT_PLAN_CODES),
            None,
        )

    html = _templates(request).render(
        request,
        "platform/billing/dashboard.html",
        {
            "identity": identity,
            "tenant": ctx.tenant,
            "billing_choices": ctx.choices,
            "tenant_qs": ctx.tenant_qs,
            "subscription": subscription,
            "current_plan": current_plan,
            "plans": plans,
            "invoices": invoices,
            "usage": usage,
            "stripe_enabled": settings.stripe_enabled,
            # Dev/staging click-through demo vs. production "we invoice
            # by bank transfer" (D2). Only one of the two is ever shown.
            "demo_checkout_allowed": settings.stripe_enabled
            or settings.billing_demo_checkout_allowed,
            "operator_email": settings.platform_operator_email,
            "live_stripe_sub": live_stripe_sub,
            "continue_plan": continue_plan,
            "offline_checkout": checkout == "offline",
            "offline_plan": offline_plan,
            "resume_plan": resume_plan,
            "non_checkout_codes": NON_CHECKOUT_PLAN_CODES,
            "billing_details_present": _billing_details_present(ctx.tenant),
            "principal": None,
            "notice": notice,
            "error": error,
        },
    )
    return HTMLResponse(html)


# ---------------------------------------------------------- checkout start


async def _run_checkout(
    request: Request,
    *,
    ctx: BillingContext,
    plan_code: str,
    identity: Identity,
    db: AsyncSession,
    settings: Settings,
) -> RedirectResponse:
    """Buy, keep or change a plan for ``ctx.tenant``. Always redirects.

    Decision order:

    1. Self-host / price-on-request plans are never sold here.
    2. A platform-admin suspension blocks any purchase.
    3. A live Stripe subscription is **modified in place** (Codex-1) —
       never a second Checkout.
    4. Without Stripe: dev/staging switch the plan locally ("demo");
       production refuses and explains that we invoice by bank
       transfer (D2) — a plan is never granted for free.
    5. Otherwise one serialized Stripe Checkout session.
    """
    tenant = ctx.tenant
    # Hosted billing has no checkout flow for the self-host pitch
    # ("community"). If a stale URL or curl points us at one of those
    # codes, refuse explicitly with a friendly message instead of
    # raising a 500 from the BillingError later in create_checkout_session.
    if plan_code in HIDDEN_PLAN_CODES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Plan '{plan_code}' is not a hosted choice. "
                "To self-host, see the installation guide at /self-hosted."
            ),
        )
    plan = await get_plan_by_code(db, plan_code)
    if plan is None or not plan.is_active:
        raise HTTPException(status_code=404, detail="Unknown plan")
    if plan.code in NON_CHECKOUT_PLAN_CODES:
        # Enterprise = price on request. A hand-made POST used to grant
        # it (unlimited caps) for free in demo mode (BIZ-01).
        return _redirect(
            ctx.url(
                error=_t(request, "This plan is arranged individually — write to {email}.").format(
                    email=settings.platform_operator_email
                )
            )
        )

    await _lock_tenant_billing(db, tenant.id)
    subscription = await get_subscription_for_tenant(db, tenant.id)

    if subscription is not None and subscription.operator_suspended_at is not None:
        return _redirect(
            ctx.url(
                error=_t(
                    request,
                    "This portal is suspended. Please write to {email}.",
                ).format(email=settings.platform_operator_email)
            )
        )

    # -- 3. existing Stripe subscription → change it in place
    if (
        settings.stripe_enabled
        and subscription is not None
        and subscription.stripe_subscription_id
        and subscription.status not in _ENDED_STRIPE_STATUSES
    ):
        if subscription.status in BLOCKED_STRIPE_STATUSES:
            return _redirect(
                ctx.url(
                    error=_t(
                        request,
                        "Your last payment did not go through. Please update your payment "
                        "method under “Manage subscription” first.",
                    )
                )
            )
        if subscription.plan_id == plan.id and not subscription.cancel_at_period_end:
            return _redirect(ctx.url(notice=_t(request, "You are already on this plan.")))
        try:
            await change_subscription_plan(
                db,
                settings,
                subscription=subscription,
                plan=plan,
                actor_label=identity.email,
            )
        except BillingError as exc:
            await db.rollback()
            log.error("billing.plan_change_failed", tenant_id=str(ctx.tenant_id), error=str(exc))
            return _redirect(
                ctx.url(
                    error=_t(
                        request,
                        "The plan change did not go through. Please try again or write to {email}.",
                    ).format(email=settings.platform_operator_email)
                )
            )
        await db.commit()
        return _redirect(
            ctx.url(
                notice=_t(
                    request,
                    "Plan changed to {plan}. The price difference is settled pro rata on "
                    "your next invoice.",
                ).format(plan=plan.name)
            )
        )

    # -- 4. no Stripe configured
    if not settings.stripe_enabled:
        if not settings.billing_demo_checkout_allowed:
            # Production without Stripe (D2): never flip the plan, never
            # say "success". The customer wants to pay — that is a lead.
            log.warning(
                "billing.checkout_offline",
                tenant_id=str(tenant.id),
                tenant_slug=tenant.slug,
                plan=plan.code,
                identity=identity.email,
            )
            await db.commit()  # release the advisory lock
            return _redirect(ctx.url(checkout="offline", plan=plan.code))

        # Dev / staging demo: flip the local subscription immediately.
        if subscription is not None:
            await set_subscription_plan(db, subscription=subscription, plan=plan, status="demo")
        _clear_selected_plan(tenant)
        await db.commit()
        return _redirect(f"{settings.app_base_url.rstrip('/')}" + ctx.url(checkout="success"))

    # -- 5. live Stripe Checkout
    # Gate Stripe checkout on Czech billing details (IČO, fakturační
    # název, adresa). Without these the locally-rendered tax doklad
    # cannot be generated. ``next`` is a GET page — the checkout itself
    # is POST-only, so we come back to the dashboard, which offers a
    # "continue to payment" button for ``checkout_plan``.
    if not _billing_details_present(tenant):
        await db.commit()
        return _redirect(
            "/platform/billing/details?next=" + quote(ctx.url(checkout_plan=plan.code))
        )

    base = settings.app_base_url.rstrip("/")
    try:
        checkout_url = await open_checkout_session(
            db,
            settings,
            tenant=tenant,
            subscription=subscription,
            plan=plan,
            success_url=base + ctx.url(checkout="success"),
            cancel_url=base + ctx.url(checkout="cancel"),
            customer_email=identity.email,
        )
    except CheckoutInProgress:
        await db.rollback()
        return _redirect(
            ctx.url(
                notice=_t(
                    request,
                    "Your payment is being confirmed. Refresh this page in a minute.",
                )
            )
        )
    except Exception as exc:
        # BillingError (configuration) or a Stripe SDK error (network,
        # API). Either way the customer gets a friendly page, not a 500
        # (UX-01); the operator gets the log line.
        await db.rollback()
        log.error(
            "checkout.failed", tenant_id=str(ctx.tenant_id), error=f"{type(exc).__name__}: {exc}"
        )
        return _redirect(
            ctx.url(
                error=_t(
                    request,
                    "Online payment could not be started. Please try again in a moment or "
                    "write to {email}.",
                ).format(email=settings.platform_operator_email)
            )
        )

    _clear_selected_plan(tenant)
    await db.commit()
    return _redirect(checkout_url)


def _clear_selected_plan(tenant: Tenant) -> None:
    """Drop the plan picked on /pricing at signup (round-3 Backend P2)."""
    t_settings = getattr(tenant, "settings", None)
    if t_settings and "selected_plan" in t_settings:
        new_settings = dict(t_settings)
        new_settings.pop("selected_plan", None)
        tenant.settings = new_settings


@csrf_router.post("/platform/billing/checkout/{plan_code}")
async def start_checkout(
    plan_code: str,
    request: Request,
    tenant: str | None = None,
    identity: Identity = Depends(require_verified_identity),
    db: AsyncSession = Depends(get_platform_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    ctx = await _resolve_billing_context(db, identity, request, tenant)
    return await _run_checkout(
        request, ctx=ctx, plan_code=plan_code, identity=identity, db=db, settings=settings
    )


# --------------------------------------------- cancel paid subscription


@csrf_router.post("/platform/billing/cancel-subscription")
async def cancel_subscription_route(
    request: Request,
    tenant: str | None = None,
    identity: Identity = Depends(require_verified_identity),
    db: AsyncSession = Depends(get_platform_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """End the paid subscription. There is no free hosted fallback.

    After the paid period ends the tenant has a short grace period (see
    ``CANCEL_GRACE_DAYS`` in the service module — currently 3 days) to
    export their data. After grace, the periodic
    ``enforce_canceled_subscriptions`` job hard-cuts access by
    deactivating the tenant.

    * Demo mode / no Stripe sub: status flips locally to canceled,
      grace clock starts now (or at the end of time already granted).
    * Live mode with Stripe sub: schedules cancel-at-period-end with
      Stripe. User keeps full access until Stripe's natural period end;
      the ``customer.subscription.deleted`` webhook then transitions
      to canceled and the periodic job takes over from there.
    * Repeated cancel: no-op, the access end does not move (Codex-7).
    """
    ctx = await _resolve_billing_context(db, identity, request, tenant)
    await _lock_tenant_billing(db, ctx.tenant.id)

    subscription = await get_subscription_for_tenant(db, ctx.tenant.id)
    if subscription is None:
        return _redirect(ctx.url(notice=_t(request, "No active subscription to cancel.")))

    try:
        outcome, access_ends_at = await cancel_subscription(
            db,
            settings,
            subscription=subscription,
            actor_label=identity.email,
        )
    except BillingError as exc:
        await db.rollback()
        log.error("billing.cancel_failed", tenant_id=str(ctx.tenant_id), error=str(exc))
        return _redirect(
            ctx.url(
                error=_t(
                    request,
                    "The cancellation did not go through. Please try again or write to {email}.",
                ).format(email=settings.platform_operator_email)
            )
        )

    await db.commit()

    date = access_ends_at.strftime("%d.%m.%Y") if access_ends_at is not None else None
    if outcome == "already":
        msg = (
            _t(request, "The subscription is already canceled. Access ends on {date}.").format(
                date=date
            )
            if date
            else _t(request, "The subscription is already canceled.")
        )
    elif outcome == "scheduled" and date:
        msg = _t(
            request,
            "Subscription canceled — you keep full access until {date}, then you have 3 days "
            "to export your data before access ends.",
        ).format(date=date)
    elif outcome == "scheduled":
        msg = _t(
            request,
            "Subscription canceled — you keep full access until the end of the current "
            "billing period, then you have 3 days to export your data.",
        )
    elif date:
        msg = _t(
            request,
            "Subscription canceled — please export your data by {date}. After that, contact "
            "{email} within 30 days for manual recovery.",
        ).format(date=date, email=settings.platform_operator_email)
    else:
        msg = _t(request, "Subscription canceled.")

    return _redirect(ctx.url(notice=msg))


# ----------------------------------------------- billing details (IČO/DIČ)


@router.get("/platform/billing/details", response_class=HTMLResponse)
async def billing_details_form(
    request: Request,
    next: str = "/platform/billing",
    notice: str | None = None,
    error: str | None = None,
    tenant: str | None = None,
    identity: Identity = Depends(require_verified_identity),
    db: AsyncSession = Depends(get_platform_db),
) -> HTMLResponse:
    """Form for the Czech-tax billing identity (IČO, DIČ, fakturační
    adresa). Required before any Stripe checkout — the locally-rendered
    daňový doklad cannot be issued without these.
    """
    ctx = await _resolve_billing_context(db, identity, request, tenant)

    blob = ctx.tenant.settings or {}
    html = _templates(request).render(
        request,
        "platform/billing/details.html",
        {
            "identity": identity,
            "tenant": ctx.tenant,
            "tenant_qs": ctx.tenant_qs,
            "form": {
                "billing_name": blob.get("billing_name") or ctx.tenant.name,
                "billing_ico": blob.get("billing_ico") or "",
                "billing_dic": blob.get("billing_dic") or "",
                "billing_address": blob.get("billing_address") or "",
            },
            "next": next,
            "notice": notice,
            "error": error,
            "principal": None,
        },
    )
    return HTMLResponse(html)


@csrf_router.post("/platform/billing/details")
async def billing_details_save(
    request: Request,
    billing_name: str = Form(""),
    billing_ico: str = Form(""),
    billing_dic: str = Form(""),
    billing_address: str = Form(""),
    next: str = Form("/platform/billing"),
    tenant: str | None = None,
    identity: Identity = Depends(require_verified_identity),
    db: AsyncSession = Depends(get_platform_db),
) -> Response:
    from app.routers.public import _safe_next_path

    ctx = await _resolve_billing_context(db, identity, request, tenant)
    details_url = ctx.url("/platform/billing/details")
    sep = "&" if "?" in details_url else "?"

    cleaned = {
        "billing_name": billing_name.strip(),
        "billing_ico": billing_ico.strip(),
        "billing_dic": billing_dic.strip(),
        "billing_address": billing_address.strip(),
    }

    # IČO must be 8 digits; DIČ optional but if present, validate basic shape.
    if not cleaned["billing_name"]:
        return _redirect(f"{details_url}{sep}error={quote('Fakturační název je povinný.')}")
    if not (cleaned["billing_ico"].isdigit() and len(cleaned["billing_ico"]) == 8):
        return _redirect(f"{details_url}{sep}error={quote('IČO musí být přesně 8 číslic.')}")
    if not cleaned["billing_address"]:
        return _redirect(f"{details_url}{sep}error={quote('Fakturační adresa je povinná.')}")
    if cleaned["billing_dic"] and not (
        cleaned["billing_dic"].upper().startswith("CZ")
        and cleaned["billing_dic"][2:].isdigit()
        and 8 <= len(cleaned["billing_dic"]) - 2 <= 10
    ):
        return _redirect(
            f"{details_url}{sep}error={quote('DIČ musí být ve formátu CZ + 8 až 10 číslic.')}"
        )

    # Re-load the tenant under this session so SQLAlchemy emits the UPDATE.
    from sqlalchemy import select

    from app.services import audit_service
    from app.services.audit_service import ActorInfo

    row = (await db.execute(select(Tenant).where(Tenant.id == ctx.tenant.id))).scalar_one()
    old_settings = dict(row.settings or {})
    new_settings = dict(old_settings)
    new_settings.update(cleaned)
    if cleaned["billing_dic"]:
        new_settings["billing_dic"] = cleaned["billing_dic"].upper()
    row.settings = new_settings
    await db.flush()

    # Append a tenant-scoped audit row so 'who changed our IČO?' has an
    # answer. Only emit when at least one of the four billing keys
    # actually changed value — pure form-resubmits don't need a row.
    keys = ("billing_name", "billing_ico", "billing_dic", "billing_address")
    before_subset = {k: old_settings.get(k) for k in keys}
    after_subset = {k: new_settings.get(k) for k in keys}
    if before_subset != after_subset:
        await audit_service.record(
            db,
            action="tenant.settings_updated",
            entity_type="tenant",
            entity_id=ctx.tenant.id,
            entity_label=ctx.tenant.slug,
            actor=ActorInfo(type="user", id=ctx.user.id, label=identity.email),
            before=before_subset,
            after=after_subset,
            tenant_id=ctx.tenant.id,
        )

    await db.commit()

    safe_next = _safe_next_path(next) or "/platform/billing"
    if safe_next == "/":
        safe_next = ctx.url()
    sep = "&" if "?" in safe_next else "?"
    return _redirect(f"{safe_next}{sep}notice={quote('Fakturační údaje uloženy.')}")


# ------------------------------------------------- post-verify checkout


@csrf_router.post("/platform/billing/post-verify-checkout/{plan_code}")
async def post_verify_checkout(
    plan_code: str,
    request: Request,
    tenant: str | None = None,
    identity: Identity = Depends(require_verified_identity),
    db: AsyncSession = Depends(get_platform_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Single-step "finish signup → checkout" for an explicit payment click.

    Round-3 audit **UX-P0** fix: one POST mints the tenant session and
    runs the checkout, so the browser never dereferences the POST-only
    checkout URL as a GET.

    Since the 2026-10-03 audit (UX-01) the verify-email page no longer
    makes this the primary action — "30 days free, no card" means the
    main button goes into the app. This endpoint stays for the
    secondary "set up payment now" path and goes through exactly the
    same checkout rules as the dashboard (including the production
    no-Stripe refusal, D2).
    """
    from app.security.session import SessionData, write_session

    # Single resolver, shared with the dashboard: it skips customer
    # contacts, non-admin / disabled staff AND platform support-access
    # grants, and walks memberships in a deterministic order.
    ctx = await _resolve_billing_context(db, identity, request, tenant)

    response = await _run_checkout(
        request, ctx=ctx, plan_code=plan_code, identity=identity, db=db, settings=settings
    )
    # Mint tenant-local session so the downstream billing dashboard /
    # app routes recognise this user without a second login.
    session_data = SessionData(
        principal_type="user",
        principal_id=str(ctx.user_id),
        tenant_id=str(ctx.tenant_id),
        customer_id=None,
        mfa_passed=False,
        session_version=ctx.user_session_version,
    )
    write_session(
        response,
        settings.app_secret_key,
        session_data,
        secure=settings.is_production,
    )
    # Same-origin fallback for in-process tests (matches switch_to_tenant).
    response.headers["X-Tenant-Slug"] = ctx.slug
    return response


# -------------------------------------------------------- customer portal


@csrf_router.post("/platform/billing/portal")
async def billing_portal(
    request: Request,
    tenant: str | None = None,
    identity: Identity = Depends(require_verified_identity),
    db: AsyncSession = Depends(get_platform_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Redirect the signed-in owner to the Stripe Customer Portal.

    Stripe hosts a full self-service UI for card updates, invoices and
    cancellation. We just mint a short-lived session URL and bounce
    the user there; Stripe emails receipts and fires our webhooks on
    the way back.

    In demo mode we simply redirect back to the billing dashboard so
    the button doesn't 404.
    """
    ctx = await _resolve_billing_context(db, identity, request, tenant)

    return_url = f"{settings.app_base_url.rstrip('/')}{ctx.url()}"

    customer_id = getattr(ctx.tenant, "stripe_customer_id", None)
    if not customer_id or not settings.stripe_enabled:
        # No Stripe customer yet (tenant never went through live checkout)
        # or demo mode — just bounce back to our dashboard.
        return _redirect(return_url)

    try:
        portal_url = create_billing_portal_session(
            settings,
            stripe_customer_id=customer_id,
            return_url=return_url,
        )
    except Exception as exc:
        log.error("billing.portal_failed", tenant_id=str(ctx.tenant.id), error=str(exc))
        return _redirect(
            ctx.url(
                error=_t(
                    request,
                    "The Stripe portal is not reachable right now. Please try again in a moment.",
                )
            )
        )

    return _redirect(portal_url)


# ----------------------------------------------------------------- webhooks


@router.post("/platform/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    db: AsyncSession = Depends(get_platform_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Receive Stripe webhook events.

    Flow:
      1. Demo mode? → 503.
      2. Verify the signature (400 on mismatch, and always 400 when
         ``STRIPE_WEBHOOK_SECRET`` is empty — SEC-1).
      3. INSERT ``event.id`` into ``platform_stripe_events`` with
         ``ON CONFLICT DO NOTHING``. Duplicate delivery (Stripe retries
         any non-2xx, and occasionally re-fires after 2xx) short-circuits
         to 200 without re-running the handler.
      4. Dispatch to the right handler based on ``event.type``.

    Not CSRF-protected: Stripe signs the payload with a shared secret,
    which :func:`verify_webhook` validates.
    """
    if not settings.stripe_enabled:
        return Response(status_code=503)

    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = verify_webhook(settings, payload, sig)
    except BillingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ``construct_event`` returns a ``stripe.Event`` (a ``StripeObject``)
    # which supports ``__getitem__`` but NOT ``.get()``. Normalise to a
    # plain dict so downstream handlers can use ordinary dict helpers.
    if hasattr(event, "to_dict"):
        event = event.to_dict()

    event_id = str(event.get("id", ""))
    event_type = str(event.get("type", ""))
    if not event_id:
        raise HTTPException(status_code=400, detail="Missing event id")

    # Explicit transaction block — gets us deterministic rollback
    # semantics on ``WebhookNotYetReady``. Without this we'd be relying
    # on SQLAlchemy's auto-begin: it works today but silently breaks if
    # a future refactor changes how ``get_platform_db`` yields sessions.
    # Round-3 audit P1-#1.
    from app.platform.billing.webhooks import WebhookNotYetReady, dispatch_webhook

    try:
        async with db.begin():
            # Dedup. ``INSERT … ON CONFLICT DO NOTHING RETURNING id`` is
            # atomic against parallel deliveries — whichever transaction
            # wins the row lock returns ``id``; the loser returns nothing
            # and short-circuits to 200.
            dedup = await db.execute(
                text(
                    "INSERT INTO platform_stripe_events (id, type, received_at) "
                    "VALUES (:id, :type, now()) ON CONFLICT (id) DO NOTHING RETURNING id"
                ),
                {"id": event_id, "type": event_type},
            )
            if dedup.scalar() is None:
                # Already processed — transaction commits on ``async with``
                # exit but that's a no-op (empty INSERT, no other writes).
                return Response(status_code=200)
            await dispatch_webhook(db, event)
            # Implicit commit on ``async with`` exit.
    except WebhookNotYetReady:
        # ``async with db.begin():`` rolls back automatically on the
        # exception; the dedup row does not survive, so Stripe's retry
        # will get a fresh attempt.
        return Response(status_code=503)

    return Response(status_code=200)


# Combine the two routers so the caller just mounts ``billing.router``.
router.include_router(csrf_router)
