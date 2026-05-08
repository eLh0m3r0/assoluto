"""Billing routes: subscription dashboard, checkout, webhooks.

All routes require an authenticated platform Identity (``require_identity``).
The dashboard shows the tenant's current plan, trial status, usage snapshot,
and invoice history; checkout kicks off a Stripe Checkout session (or
pretends to in demo mode).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models.tenant import Tenant
from app.models.user import User
from app.platform.billing.service import (
    HIDDEN_PLAN_CODES,
    BillingError,
    cancel_subscription,
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


# Keys we expect the operator to have filled before any Stripe checkout
# can run. ``billing_ico`` is the Czech company ID — without it the
# generated tax doklad is invalid. The other two are needed for the
# fakturační adresa block. ``billing_dic`` is optional (a non-VAT-payer
# can sell without one).
REQUIRED_BILLING_KEYS: tuple[str, ...] = ("billing_ico", "billing_name", "billing_address")


def _billing_details_present(tenant: Tenant) -> bool:
    blob = tenant.settings or {}
    return all(str(blob.get(k) or "").strip() for k in REQUIRED_BILLING_KEYS)


async def _resolve_current_tenant(
    db: AsyncSession, identity: Identity
) -> tuple[Tenant, User] | tuple[None, None]:
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
    notice: str | None = None,
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

    usage = await snapshot_tenant_usage(db, tenant.id)

    if checkout == "success":
        notice = "Předplatné bylo úspěšně aktualizováno."
    # ``notice`` may also arrive as a query-string flash from the
    # cancel-subscription route; in that case keep what was sent.

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
            "billing_details_present": _billing_details_present(tenant),
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

    # Gate Stripe checkout on Czech billing details (IČO, fakturační
    # název, adresa). Without these the locally-rendered tax doklad
    # cannot be generated; the demo path is exempt because no PDF is
    # produced. See ``_billing_details_present``.
    if settings.stripe_enabled and not _billing_details_present(tenant):
        from urllib.parse import quote

        return RedirectResponse(
            url="/platform/billing/details?next="
            + quote(f"/platform/billing/checkout/{plan_code}"),
            status_code=status.HTTP_303_SEE_OTHER,
        )

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
    current_sub = await get_subscription_for_tenant(db, tenant.id)
    trial_ends_at = current_sub.trial_ends_at if current_sub else None
    subscription_id = current_sub.id if current_sub else None

    try:
        checkout_url = create_checkout_session(
            settings,
            tenant=tenant,
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
        subscription = await get_subscription_for_tenant(db, tenant.id)
        if subscription is not None:
            await set_subscription_plan(db, subscription=subscription, plan=plan, status="demo")

    # Clear any lingering ``selected_plan`` from signup so the
    # post-verify CTA doesn't keep re-surfacing after an upgrade
    # (round-3 Backend P2).
    t_settings = getattr(tenant, "settings", None)
    if t_settings and "selected_plan" in t_settings:
        new_settings = dict(t_settings)
        new_settings.pop("selected_plan", None)
        tenant.settings = new_settings

    await db.commit()

    return RedirectResponse(url=checkout_url, status_code=status.HTTP_303_SEE_OTHER)


# --------------------------------------------- cancel paid subscription


@csrf_router.post("/platform/billing/cancel-subscription")
async def cancel_subscription_route(
    request: Request,
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
      grace clock starts now.
    * Live mode with Stripe sub: schedules cancel-at-period-end with
      Stripe. User keeps full access until Stripe's natural period end;
      the ``customer.subscription.deleted`` webhook then transitions
      to canceled and the periodic job takes over from there.
    """
    from urllib.parse import quote

    tenant, _ = await _resolve_current_tenant(db, identity)
    if tenant is None:
        raise HTTPException(status_code=404, detail="No tenant to manage")

    subscription = await get_subscription_for_tenant(db, tenant.id)
    if subscription is None:
        return RedirectResponse(
            url="/platform/billing?notice=" + quote("No active subscription to cancel."),
            status_code=303,
        )

    try:
        outcome, access_ends_at = await cancel_subscription(
            db,
            settings,
            subscription=subscription,
            actor_label=identity.email,
        )
    except BillingError as exc:
        raise HTTPException(status_code=502, detail=f"Cancel failed: {exc}") from exc

    await db.commit()

    if outcome == "scheduled" and access_ends_at is not None:
        msg = (
            f"Subscription canceled — you keep full access until "
            f"{access_ends_at.date().isoformat()}, then you have 3 days "
            f"to export your data before access ends."
        )
    elif outcome == "scheduled":
        msg = (
            "Subscription canceled — you keep full access until the end of the "
            "current billing period, then you have 3 days to export your data."
        )
    elif access_ends_at is not None:
        msg = (
            f"Subscription canceled — please export your data by "
            f"{access_ends_at.date().isoformat()}. After that, contact "
            f"team@assoluto.eu within 30 days for manual recovery."
        )
    else:
        msg = "Subscription canceled."

    return RedirectResponse(
        url="/platform/billing?notice=" + quote(msg),
        status_code=303,
    )


# ----------------------------------------------- billing details (IČO/DIČ)


@router.get("/platform/billing/details", response_class=HTMLResponse)
async def billing_details_form(
    request: Request,
    next: str = "/platform/billing",
    notice: str | None = None,
    error: str | None = None,
    identity: Identity = Depends(require_verified_identity),
    db: AsyncSession = Depends(get_platform_db),
) -> HTMLResponse:
    """Form for the Czech-tax billing identity (IČO, DIČ, fakturační
    adresa). Required before any Stripe checkout — the locally-rendered
    daňový doklad cannot be issued without these.
    """
    tenant, _ = await _resolve_current_tenant(db, identity)
    if tenant is None:
        raise HTTPException(status_code=404, detail="No tenant to manage")

    blob = tenant.settings or {}
    html = _templates(request).render(
        request,
        "platform/billing/details.html",
        {
            "identity": identity,
            "tenant": tenant,
            "form": {
                "billing_name": blob.get("billing_name") or tenant.name,
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
    identity: Identity = Depends(require_verified_identity),
    db: AsyncSession = Depends(get_platform_db),
) -> Response:
    from urllib.parse import quote

    from app.routers.public import _safe_next_path

    tenant, _ = await _resolve_current_tenant(db, identity)
    if tenant is None:
        raise HTTPException(status_code=404, detail="No tenant to manage")

    cleaned = {
        "billing_name": billing_name.strip(),
        "billing_ico": billing_ico.strip(),
        "billing_dic": billing_dic.strip(),
        "billing_address": billing_address.strip(),
    }

    # IČO must be 8 digits; DIČ optional but if present, validate basic shape.
    if not cleaned["billing_name"]:
        return RedirectResponse(
            url=f"/platform/billing/details?error={quote('Fakturační název je povinný.')}",
            status_code=303,
        )
    if not (cleaned["billing_ico"].isdigit() and len(cleaned["billing_ico"]) == 8):
        return RedirectResponse(
            url=f"/platform/billing/details?error={quote('IČO musí být přesně 8 číslic.')}",
            status_code=303,
        )
    if not cleaned["billing_address"]:
        return RedirectResponse(
            url=f"/platform/billing/details?error={quote('Fakturační adresa je povinná.')}",
            status_code=303,
        )
    if cleaned["billing_dic"] and not (
        cleaned["billing_dic"].upper().startswith("CZ")
        and cleaned["billing_dic"][2:].isdigit()
        and 8 <= len(cleaned["billing_dic"]) - 2 <= 10
    ):
        return RedirectResponse(
            url=f"/platform/billing/details?error={quote('DIČ musí být ve formátu CZ + 8 až 10 číslic.')}",
            status_code=303,
        )

    # Re-load the tenant under this session so SQLAlchemy emits the UPDATE.
    from sqlalchemy import select

    row = (await db.execute(select(Tenant).where(Tenant.id == tenant.id))).scalar_one()
    new_settings = dict(row.settings or {})
    new_settings.update(cleaned)
    if cleaned["billing_dic"]:
        new_settings["billing_dic"] = cleaned["billing_dic"].upper()
    row.settings = new_settings
    await db.commit()

    safe_next = _safe_next_path(next) or "/platform/billing"
    if safe_next == "/":
        safe_next = "/platform/billing"
    sep = "&" if "?" in safe_next else "?"
    return RedirectResponse(
        url=f"{safe_next}{sep}notice={quote('Fakturační údaje uloženy.')}",
        status_code=303,
    )


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

    # Same Czech-tax billing-details gate as the dashboard checkout
    # path (see ``_billing_details_present``). Demo mode is exempt.
    if settings.stripe_enabled and not _billing_details_present(tenant_obj):
        from urllib.parse import quote

        return RedirectResponse(
            url="/platform/billing/details?next="
            + quote(f"/platform/billing/checkout/{plan_code}"),
            status_code=status.HTTP_303_SEE_OTHER,
        )

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
