"""Billing routes: subscription dashboard, checkout, webhooks.

All routes require an authenticated platform Identity (``require_identity``).
The dashboard shows the tenant's current plan, trial status, usage snapshot,
and invoice history; checkout kicks off a Stripe Checkout session (or
pretends to in demo mode).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.platform.billing.service import (
    BillingError,
    create_billing_portal_session,
    create_checkout_session,
    get_subscription_for_tenant,
    list_invoices_for_tenant,
    list_plans,
    require_plan,
    set_subscription_plan,
    verify_webhook,
)
from app.platform.deps import get_platform_db, require_verified_identity
from app.platform.models import Identity
from app.platform.service import list_memberships_for_identity
from app.security.csrf import verify_csrf

router = APIRouter(tags=["platform-billing"])

# GET routes don't need CSRF; the checkout POST + portal POST do.
csrf_router = APIRouter(tags=["platform-billing"], dependencies=[Depends(verify_csrf)])


def _templates(request: Request):
    return request.app.state.templates


async def _resolve_current_tenant(
    db: AsyncSession, identity: Identity
) -> tuple[object, object] | tuple[None, None]:
    """Pick the first tenant this identity owns as staff.

    Billing is a per-tenant concern. For identities with memberships in
    multiple tenants we use the tenant they switched into most recently
    (tracked via ``last_login_at`` is-rough-enough — real impl would
    read a session cookie). First pass: first staff membership.
    """
    from app.platform.service import resolve_membership_targets

    memberships = await list_memberships_for_identity(db, identity_id=identity.id)
    for membership in memberships:
        if membership.user_id is None:
            continue  # customer contacts don't manage billing
        tenant, target = await resolve_membership_targets(db, membership=membership)
        if tenant is not None:
            return tenant, target
    return None, None


# --------------------------------------------------------- billing dashboard


@router.get("/platform/billing", response_class=HTMLResponse)
async def billing_dashboard(
    request: Request,
    identity: Identity = Depends(require_verified_identity),
    db: AsyncSession = Depends(get_platform_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    tenant, _ = await _resolve_current_tenant(db, identity)
    if tenant is None:
        raise HTTPException(status_code=404, detail="No tenant to manage")

    subscription = await get_subscription_for_tenant(db, tenant.id)
    plans = await list_plans(db)
    invoices = await list_invoices_for_tenant(db, tenant.id)

    current_plan = None
    if subscription is not None:
        for plan in plans:
            if plan.id == subscription.plan_id:
                current_plan = plan
                break

    # Pass a usage snapshot so the dashboard can render progress bars
    # against the current plan caps.
    from app.platform.usage import snapshot_tenant_usage

    usage = await snapshot_tenant_usage(db, tenant.id)  # type: ignore[attr-defined]

    html = _templates(request).render(
        request,
        "platform/billing/dashboard.html",
        {
            "identity": identity,
            "tenant": tenant,
            "subscription": subscription,
            "current_plan": current_plan,
            "plans": plans,
            "invoices": invoices,
            "usage": usage,
            "stripe_enabled": settings.stripe_enabled,
            "principal": None,
        },
    )
    return HTMLResponse(html)


# ---------------------------------------------------------- checkout start


@csrf_router.post("/platform/billing/checkout/{plan_code}")
async def start_checkout(
    plan_code: str,
    request: Request,
    identity: Identity = Depends(require_verified_identity),
    db: AsyncSession = Depends(get_platform_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    tenant, _ = await _resolve_current_tenant(db, identity)
    if tenant is None:
        raise HTTPException(status_code=404, detail="No tenant to manage")

    plan = await require_plan(db, plan_code)
    base = settings.app_base_url.rstrip("/")
    success_url = f"{base}/platform/billing?checkout=success"
    cancel_url = f"{base}/platform/billing?checkout=cancel"

    # Carry the existing trial clock onto the Stripe side: the
    # service layer chooses ``trial_end`` (absolute) over
    # ``trial_period_days`` (always fresh) when we still have trial
    # left.
    current_sub = await get_subscription_for_tenant(db, tenant.id)  # type: ignore[attr-defined]
    trial_ends_at = current_sub.trial_ends_at if current_sub else None

    try:
        checkout_url = create_checkout_session(
            settings,
            tenant=tenant,  # type: ignore[arg-type]
            plan=plan,
            success_url=success_url,
            cancel_url=cancel_url,
            customer_email=identity.email,
            trial_ends_at=trial_ends_at,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Checkout failed: {exc}") from exc

    # In demo mode, flip the local subscription to the chosen plan immediately.
    if not settings.stripe_enabled:
        subscription = await get_subscription_for_tenant(db, tenant.id)  # type: ignore[attr-defined]
        if subscription is not None:
            await set_subscription_plan(db, subscription=subscription, plan=plan, status="demo")
            await db.commit()

    return RedirectResponse(url=checkout_url, status_code=status.HTTP_303_SEE_OTHER)


# -------------------------------------------------------- customer portal


@csrf_router.post("/platform/billing/portal")
async def billing_portal(
    request: Request,
    identity: Identity = Depends(require_verified_identity),
    db: AsyncSession = Depends(get_platform_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Redirect the signed-in owner to the Stripe Customer Portal.

    Stripe hosts a full self-service UI for plan changes, card updates,
    and cancellation. We just mint a short-lived session URL and bounce
    the user there; Stripe emails receipts and fires our webhooks on
    the way back.

    In demo mode we simply redirect back to the billing dashboard so
    the button doesn't 404.
    """
    tenant, _ = await _resolve_current_tenant(db, identity)
    if tenant is None:
        raise HTTPException(status_code=404, detail="No tenant to manage")

    return_url = f"{settings.app_base_url.rstrip('/')}/platform/billing"

    customer_id = getattr(tenant, "stripe_customer_id", None)
    if not customer_id or not settings.stripe_enabled:
        # No Stripe customer yet (tenant never went through live checkout)
        # or demo mode — just bounce back to our dashboard.
        return RedirectResponse(url=return_url, status_code=status.HTTP_303_SEE_OTHER)

    try:
        portal_url = create_billing_portal_session(
            settings,
            stripe_customer_id=customer_id,
            return_url=return_url,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Billing portal failed: {exc}") from exc

    return RedirectResponse(url=portal_url, status_code=status.HTTP_303_SEE_OTHER)


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
      2. Verify the signature (400 on mismatch).
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

    # Dedup. Using raw SQL + ON CONFLICT keeps this atomic against
    # parallel webhook deliveries.
    from sqlalchemy import text

    dedup = await db.execute(
        text(
            "INSERT INTO platform_stripe_events (id, type, received_at) "
            "VALUES (:id, :type, now()) ON CONFLICT (id) DO NOTHING RETURNING id"
        ),
        {"id": event_id, "type": event_type},
    )
    if dedup.scalar() is None:
        # Already processed — return 200 immediately.
        await db.commit()
        return Response(status_code=200)

    from app.platform.billing.webhooks import dispatch_webhook

    await dispatch_webhook(db, event)
    await db.commit()
    return Response(status_code=200)


# Combine the two routers so the caller just mounts ``billing.router``.
router.include_router(csrf_router)
