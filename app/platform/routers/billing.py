"""Billing routes: subscription dashboard, checkout, webhooks.

All routes require an authenticated platform Identity (``require_identity``).
The dashboard shows the tenant's current plan, trial status, usage snapshot,
and invoice history; checkout kicks off a Stripe Checkout session (or
pretends to in demo mode).
"""

from __future__ import annotations

from uuid import UUID

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
    """Pick the first tenant this identity owns **as tenant_admin**.

    Billing is a per-tenant concern AND a privileged one: cancelling
    a subscription or switching plans has real money consequences. We
    therefore require ``UserRole.TENANT_ADMIN`` on the membership's
    user — plain tenant_staff memberships are skipped even if they
    appear first in the list.

    Customer contact memberships are likewise skipped (customers should
    never see their supplier's billing dashboard).
    """
    from app.models.enums import UserRole
    from app.models.user import User
    from app.platform.service import resolve_membership_targets

    memberships = await list_memberships_for_identity(db, identity_id=identity.id)
    for membership in memberships:
        if membership.user_id is None:
            continue  # customer contacts don't manage billing
        tenant, target = await resolve_membership_targets(db, membership=membership)
        if tenant is None or not isinstance(target, User):
            continue
        if target.role != UserRole.TENANT_ADMIN:
            continue
        return tenant, target
    return None, None


# --------------------------------------------------------- billing dashboard


@router.get("/platform/billing/invoices/{invoice_id}.pdf")
async def invoice_pdf(
    invoice_id: UUID,
    request: Request,
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

    from app.models.tenant import Tenant
    from app.platform.billing.models import Invoice
    from app.services.invoice_pdf_service import _safe_filename_for, render_invoice_pdf

    tenant, _ = await _resolve_current_tenant(db, identity)
    if tenant is None:
        raise HTTPException(status_code=404, detail="No tenant to manage")

    invoice = (
        await db.execute(
            select(Invoice).where(Invoice.id == invoice_id, Invoice.tenant_id == tenant.id)
        )
    ).scalar_one_or_none()
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # Re-fetch tenant as the ORM row so its ``settings`` JSONB is accessible.
    tenant_row = (await db.execute(select(Tenant).where(Tenant.id == tenant.id))).scalar_one()

    pdf_bytes = render_invoice_pdf(invoice=invoice, tenant=tenant_row, settings=settings)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{_safe_filename_for(invoice)}"'},
    )


@router.get("/platform/billing", response_class=HTMLResponse)
async def billing_dashboard(
    request: Request,
    checkout: str | None = None,
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

    notice = None
    if checkout == "success":
        notice = "Předplatné bylo úspěšně aktualizováno."

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
            "notice": notice,
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
    # left. ``subscription_id`` anchors the Stripe idempotency key
    # for post-trial retries so repeat upgrade attempts don't
    # collide with a stale cached session (round-3 P1-#2).
    current_sub = await get_subscription_for_tenant(db, tenant.id)  # type: ignore[attr-defined]
    trial_ends_at = current_sub.trial_ends_at if current_sub else None
    subscription_id = current_sub.id if current_sub else None

    try:
        checkout_url = create_checkout_session(
            settings,
            tenant=tenant,  # type: ignore[arg-type]
            plan=plan,
            success_url=success_url,
            cancel_url=cancel_url,
            customer_email=identity.email,
            trial_ends_at=trial_ends_at,
            subscription_id=subscription_id,
        )
    except BillingError as exc:
        raise HTTPException(status_code=500, detail=f"Checkout failed: {exc}") from exc
    except Exception as exc:
        # Stripe SDK errors surface via ``stripe.error.StripeError``
        # (imported lazily). Map cleanly to 502 (upstream issue) vs
        # 500 (our bug).
        from app.logging import get_logger

        get_logger("app.billing").error(
            "checkout.failed", tenant_id=str(tenant.id), error=f"{type(exc).__name__}: {exc}"
        )
        raise HTTPException(
            status_code=502, detail="Payment provider error, please try again."
        ) from exc

    # In demo mode, flip the local subscription to the chosen plan immediately.
    if not settings.stripe_enabled:
        subscription = await get_subscription_for_tenant(db, tenant.id)  # type: ignore[attr-defined]
        if subscription is not None:
            await set_subscription_plan(db, subscription=subscription, plan=plan, status="demo")

    # Clear any lingering ``selected_plan`` from signup so the
    # post-verify CTA doesn't keep re-surfacing after an upgrade
    # (round-3 Backend P2).
    t_settings = getattr(tenant, "settings", None)
    if t_settings and "selected_plan" in t_settings:
        new_settings = dict(t_settings)
        new_settings.pop("selected_plan", None)
        tenant.settings = new_settings  # type: ignore[attr-defined]

    await db.commit()

    return RedirectResponse(url=checkout_url, status_code=status.HTTP_303_SEE_OTHER)


# ------------------------------------------------- post-verify checkout


@csrf_router.post("/platform/billing/post-verify-checkout/{plan_code}")
async def post_verify_checkout(
    plan_code: str,
    request: Request,
    identity: Identity = Depends(require_verified_identity),
    db: AsyncSession = Depends(get_platform_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Single-step "finish signup → Stripe Checkout" for the post-verify CTA.

    Round-3 audit **UX-P0** fix. Previous flow was:
      1. verify_email POSTs /platform/switch/{slug} with ``next=/platform/billing/checkout/{plan}``
      2. switch mints a tenant session and 303s to that next URL
      3. **Browser follows with GET** → 405 Method Not Allowed
         (the checkout endpoint is POST-only, CSRF-gated)

    This endpoint collapses all three steps into one POST so the
    browser never dereferences the checkout URL as a GET. It:
      1. Resolves the owner's tenant via platform membership
      2. Mints a tenant-local session cookie
      3. Creates a Stripe Checkout session (or demo-mode switch)
      4. Clears ``tenant.settings["selected_plan"]`` so a replay of
         verify-email doesn't keep showing the "Finish setting up"
         CTA forever
      5. 303 to the final Stripe URL
    """
    from app.platform.service import list_memberships_for_identity, resolve_membership_targets
    from app.security.session import SessionData, write_session

    memberships = await list_memberships_for_identity(db, identity_id=identity.id)
    tenant_obj = None
    user_target = None
    for membership in memberships:
        if membership.user_id is None:
            continue
        t_candidate, target = await resolve_membership_targets(db, membership=membership)
        if t_candidate is None:
            continue
        from app.models.enums import UserRole
        from app.models.user import User

        if isinstance(target, User) and target.role == UserRole.TENANT_ADMIN:
            tenant_obj = t_candidate
            user_target = target
            break
    if tenant_obj is None or user_target is None:
        raise HTTPException(status_code=404, detail="No tenant to manage")

    plan = await require_plan(db, plan_code)
    base = settings.app_base_url.rstrip("/")
    success_url = f"{base}/platform/billing?checkout=success"
    cancel_url = f"{base}/platform/billing?checkout=cancel"

    current_sub = await get_subscription_for_tenant(db, tenant_obj.id)
    trial_ends_at = current_sub.trial_ends_at if current_sub else None
    subscription_id = current_sub.id if current_sub else None

    try:
        checkout_url = create_checkout_session(
            settings,
            tenant=tenant_obj,
            plan=plan,
            success_url=success_url,
            cancel_url=cancel_url,
            customer_email=identity.email,
            trial_ends_at=trial_ends_at,
            subscription_id=subscription_id,
        )
    except BillingError as exc:
        raise HTTPException(status_code=500, detail=f"Checkout failed: {exc}") from exc
    except Exception as exc:
        from app.logging import get_logger

        get_logger("app.billing").error(
            "post_verify_checkout.failed",
            tenant_id=str(tenant_obj.id),
            error=f"{type(exc).__name__}: {exc}",
        )
        raise HTTPException(
            status_code=502, detail="Payment provider error, please try again."
        ) from exc

    # Demo mode: flip local subscription immediately so the "Finish
    # setting up" CTA doesn't silently no-op. Same UX promise as the
    # regular /platform/billing/checkout/{plan} path.
    if not settings.stripe_enabled and current_sub is not None:
        await set_subscription_plan(db, subscription=current_sub, plan=plan, status="demo")

    # Clear ``selected_plan`` so a later verify-email replay doesn't
    # keep re-offering the CTA. Best-effort — if the JSON dict is
    # missing the key, nothing happens.
    if tenant_obj.settings and "selected_plan" in tenant_obj.settings:
        new_settings = dict(tenant_obj.settings)
        new_settings.pop("selected_plan", None)
        tenant_obj.settings = new_settings

    await db.commit()

    response = RedirectResponse(url=checkout_url, status_code=status.HTTP_303_SEE_OTHER)
    # Mint tenant-local session so the downstream billing dashboard /
    # app routes recognise this user without a second login.
    session_data = SessionData(
        principal_type="user",
        principal_id=str(user_target.id),
        tenant_id=str(tenant_obj.id),
        customer_id=None,
        mfa_passed=False,
        session_version=user_target.session_version,
    )
    write_session(
        response,
        settings.app_secret_key,
        session_data,
        secure=settings.is_production,
    )
    # Same-origin fallback for in-process tests (matches switch_to_tenant).
    response.headers["X-Tenant-Slug"] = tenant_obj.slug
    return response


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

    # Explicit transaction block — gets us deterministic rollback
    # semantics on ``WebhookNotYetReady``. Without this we'd be relying
    # on SQLAlchemy's auto-begin: it works today but silently breaks if
    # a future refactor changes how ``get_platform_db`` yields sessions.
    # Round-3 audit P1-#1.
    from sqlalchemy import text

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
