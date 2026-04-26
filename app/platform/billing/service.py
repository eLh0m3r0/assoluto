"""Billing service — demo + live mode helpers.

All Stripe API calls are funnelled through ``_get_stripe()`` which lazily
imports the ``stripe`` package and configures the API key. In demo mode
(no ``STRIPE_SECRET_KEY`` set) ``_get_stripe()`` returns ``None`` and the
caller falls back to local-only bookkeeping.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.tenant import Tenant
from app.platform.billing.models import Invoice, Plan, Subscription

TRIAL_DAYS = 30


class BillingError(Exception):
    pass


class PlanNotFound(BillingError):
    pass


class SubscriptionNotFound(BillingError):
    pass


# ----------------------------------------------------------------- plans


async def get_plan_by_code(db: AsyncSession, code: str) -> Plan | None:
    return (await db.execute(select(Plan).where(Plan.code == code))).scalar_one_or_none()


# Plans that exist in the DB but are NOT shown as a hosted-tenant choice.
# ``community`` is the AGPL self-host pitch on the marketing site (see
# /pricing → "Installation guide" CTA). On the hosted SaaS it has no
# meaning — there is no free hosted tier — so it stays out of the
# billing-dashboard plan grid and out of any checkout/upgrade flow.
HIDDEN_PLAN_CODES: frozenset[str] = frozenset({"community"})


async def list_plans(db: AsyncSession) -> list[Plan]:
    """Active plans visible to hosted tenants — community deliberately
    excluded (see HIDDEN_PLAN_CODES).
    """
    result = await db.execute(
        select(Plan)
        .where(Plan.is_active.is_(True))
        .where(Plan.code.notin_(HIDDEN_PLAN_CODES))
        .order_by(Plan.monthly_price_cents)
    )
    return list(result.scalars().all())


async def require_plan(db: AsyncSession, code: str) -> Plan:
    plan = await get_plan_by_code(db, code)
    if plan is None:
        raise PlanNotFound(code)
    return plan


# --------------------------------------------------------- subscriptions


async def get_subscription_for_tenant(db: AsyncSession, tenant_id: UUID) -> Subscription | None:
    return (
        await db.execute(select(Subscription).where(Subscription.tenant_id == tenant_id))
    ).scalar_one_or_none()


async def start_trial_subscription(
    db: AsyncSession,
    *,
    tenant: Tenant,
    plan_code: str = "starter",
) -> Subscription:
    """Attach a trial subscription to a brand-new tenant.

    Called from the self-signup flow right after the Tenant is created.
    Uses ``plan_code`` as the "intended" post-trial plan. When the
    trial ends without an active Stripe subscription,
    ``expire_demo_trials`` flips status to ``canceled`` (plan_id stays
    as a historical record); the tenant then has CANCEL_GRACE_DAYS to
    convert before ``enforce_canceled_subscriptions`` deactivates them.
    """
    plan = await require_plan(db, plan_code)
    existing = await get_subscription_for_tenant(db, tenant.id)
    if existing is not None:
        return existing

    now = datetime.now(UTC)
    subscription = Subscription(
        tenant_id=tenant.id,
        plan_id=plan.id,
        status="trialing",
        trial_ends_at=now + timedelta(days=TRIAL_DAYS),
        current_period_start=now,
        current_period_end=now + timedelta(days=TRIAL_DAYS),
    )
    db.add(subscription)
    await db.flush()
    return subscription


async def set_subscription_plan(
    db: AsyncSession,
    *,
    subscription: Subscription,
    plan: Plan,
    status: str | None = None,
) -> Subscription:
    subscription.plan_id = plan.id
    if status is not None:
        subscription.status = status
    await db.flush()
    return subscription


# How long a canceled tenant keeps full access after the paid period
# ends, before the periodic job (enforce_canceled_subscriptions) hard-cuts
# the tenant. Marketed as "3 days to export your data" — beyond that, we
# offer manual recovery via team@assoluto.eu for up to 30 days, then
# delete. Keep this small to avoid free-rider risk, and line it up with
# the marketing copy in pricing.html / index.html FAQ.
CANCEL_GRACE_DAYS = 3


async def cancel_subscription(
    db: AsyncSession,
    settings: Settings,
    *,
    subscription: Subscription,
    actor_label: str | None = None,
) -> tuple[str, datetime | None]:
    """End the tenant's paid subscription. After a short grace period
    the periodic ``enforce_canceled_subscriptions`` job hard-cuts the
    tenant.

    There is NO "free Community fallback" on hosted — Community is the
    self-host AGPL pitch only (see HIDDEN_PLAN_CODES). Cancel means the
    paid SaaS service ends; the tenant has ``CANCEL_GRACE_DAYS`` days
    to export data, then access is denied at the tenant subdomain.

    * Demo mode OR no Stripe subscription: status flips immediately to
      ``canceled``, ``current_period_end`` is set to ``now()`` so grace
      starts now (3 days from this call). plan_id is left as-is — the
      row records what they had; nothing automatic flips it.
    * Live mode with a Stripe sub: ``stripe.Subscription.modify(
      cancel_at_period_end=True)`` is called. The user keeps full access
      until Stripe's natural period end. The
      ``customer.subscription.deleted`` webhook then sets
      ``status='canceled'`` locally and the periodic job takes over from
      there.

    Returns ``("flipped", access_ends_at)`` for the immediate-flip path
    or ``("scheduled", stripe_period_end)`` for the Stripe-cancel path.
    The caller uses the returned datetime to tell the user when their
    access actually ends.

    ``actor_label`` is recorded on the tenant's ``audit_events`` row
    (action ``billing.subscription_canceled``) so a tenant admin can
    see who cancelled their plan. The audit row is written with an
    explicit ``tenant_id`` because this service runs on the platform
    DB session which bypasses RLS — there's no ``app.tenant_id``
    setting to read from.
    """
    from app.services import audit_service
    from app.services.audit_service import ActorInfo

    stripe = _get_stripe(settings)
    stripe_sub_id = getattr(subscription, "stripe_subscription_id", None)

    if stripe is None or not stripe_sub_id:
        # Demo mode OR live without a Stripe sub (trial that never
        # converted). Flip locally and start the grace clock now.
        now = datetime.now(UTC)
        status_before = subscription.status
        subscription.status = "canceled"
        subscription.cancel_at_period_end = False
        # Pin period_end to the natural end of the trial when one exists
        # and is still in the future; otherwise start the grace from now.
        if subscription.current_period_end is None or subscription.current_period_end < now:
            subscription.current_period_end = now
        access_ends_at = subscription.current_period_end + timedelta(days=CANCEL_GRACE_DAYS)
        await db.flush()
        await audit_service.record(
            db,
            action="billing.subscription_canceled",
            entity_type="subscription",
            entity_id=subscription.id,
            entity_label=f"plan={subscription.plan_id} status={status_before}→canceled",
            actor=ActorInfo(
                type="user",
                id=None,
                label=actor_label or "platform-identity",
            ),
            after={
                "mode": "demo",
                "access_ends_at": access_ends_at.isoformat(),
                "current_period_end": subscription.current_period_end.isoformat(),
            },
            tenant_id=subscription.tenant_id,
        )
        return ("flipped", access_ends_at)

    # Live mode with a Stripe subscription — schedule the cancel.
    # Stripe SDK may raise many error shapes (StripeError, network, JSON
    # decode); trap broadly and re-emit as our domain error so the caller
    # gets a uniform 502.
    try:
        stripe_sub = stripe.Subscription.modify(
            stripe_sub_id,
            cancel_at_period_end=True,
        )
    except Exception as exc:
        raise BillingError(f"Stripe cancel failed: {exc}") from exc

    subscription.cancel_at_period_end = True
    await db.flush()
    period_end_ts = (
        stripe_sub.get("current_period_end")
        if isinstance(stripe_sub, dict)
        else (getattr(stripe_sub, "current_period_end", None))
    )
    period_end_dt = datetime.fromtimestamp(int(period_end_ts), tz=UTC) if period_end_ts else None
    await audit_service.record(
        db,
        action="billing.subscription_canceled",
        entity_type="subscription",
        entity_id=subscription.id,
        entity_label=f"plan={subscription.plan_id} stripe_sub={stripe_sub_id}",
        actor=ActorInfo(
            type="user",
            id=None,
            label=actor_label or "platform-identity",
        ),
        after={
            "mode": "live_scheduled",
            "stripe_subscription_id": stripe_sub_id,
            "period_end": period_end_dt.isoformat() if period_end_dt else None,
        },
        tenant_id=subscription.tenant_id,
    )
    return ("scheduled", period_end_dt)


# --------------------------------------------------------- Stripe helpers


def _get_stripe(settings: Settings) -> Any | None:
    """Return a configured ``stripe`` module, or None in demo mode."""
    if not settings.stripe_enabled:
        return None
    import stripe  # local import — optional at runtime

    stripe.api_key = settings.stripe_secret_key
    return stripe


def create_checkout_session(
    settings: Settings,
    *,
    tenant: Tenant,
    plan: Plan,
    success_url: str,
    cancel_url: str,
    customer_email: str,
    trial_ends_at: datetime | None = None,
    subscription_id: UUID | None = None,
) -> str:
    """Return a URL the caller should redirect to.

    * Live mode: Stripe Checkout session URL.
    * Demo mode: a fake local URL that just bounces back to ``success_url``
      so the signup/upgrade flow is testable locally.

    ``trial_ends_at`` — the already-planned trial end (from our local
    ``Subscription.trial_ends_at``). When supplied and still in the future
    we pass it to Stripe as an explicit ``trial_end`` timestamp rather
    than a fresh 14-day window; that prevents a second trial after an
    in-app trial has already been consumed.

    ``subscription_id`` — our local ``Subscription.id``. Used as the
    idempotency-key anchor when the trial has already been consumed
    (``trial_ends_at`` is ``None`` or in the past); without it the key
    would collapse to the same ``"no-trial"`` sentinel across every
    future upgrade attempt for the same tenant, causing Stripe to
    return a stale cached session.
    """
    stripe = _get_stripe(settings)
    if stripe is None:
        # Demo mode (no Stripe configured): pretend the user clicked
        # "Pay", go straight to success. The local subscription was
        # already flipped by the caller.
        return success_url
    if not plan.stripe_price_id:
        # Live mode but the plan has no Stripe price ID. This is a
        # configuration error (env var missing, sync failed, or the
        # plan is the free Community tier which has no checkout flow
        # at all). Returning ``success_url`` here would silently no-op
        # the upgrade — the user thinks they paid, nothing happened.
        # Better to fail loud so the operator sees it.
        raise BillingError(
            f"Plan '{plan.code}' has no stripe_price_id configured — "
            "checkout cannot proceed. Set STRIPE_PRICE_* env vars or "
            "remove this plan from the upgrade UI."
        )

    # Stripe design: metadata on the Session does NOT propagate onto the
    # Subscription / Invoice the session creates — you must also set it
    # on ``subscription_data.metadata`` (per
    # https://docs.stripe.com/api/checkout/sessions/create). We set
    # ``tenant_id`` in THREE places so every downstream object we might
    # receive in a webhook can find it:
    #
    #   * ``client_reference_id``  — on ``checkout.session.completed``
    #   * ``metadata.tenant_id``   — on the Session itself
    #   * ``subscription_data.metadata.tenant_id`` — propagates to the
    #                                                 Subscription + its
    #                                                 Invoices
    tenant_meta = {"tenant_id": str(tenant.id), "plan_code": plan.code}
    # Decide on the trial handshake. Stripe accepts one of:
    #   - ``trial_period_days`` (relative): always a fresh N-day window
    #   - ``trial_end`` (absolute Unix timestamp): useful when we want
    #     the Stripe side to mirror the in-app trial clock we already
    #     started at signup. We prefer the absolute form when the
    #     local trial is still in the future, and we disable the trial
    #     entirely when it already expired.
    subscription_data: dict[str, Any] = {"metadata": tenant_meta}
    if trial_ends_at is not None and trial_ends_at > datetime.now(UTC):
        subscription_data["trial_end"] = int(trial_ends_at.timestamp())
    elif trial_ends_at is None:
        subscription_data["trial_period_days"] = TRIAL_DAYS
    # else: trial already consumed — no trial on the new checkout.

    session_kwargs: dict[str, Any] = {
        "mode": "subscription",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "line_items": [{"price": plan.stripe_price_id, "quantity": 1}],
        "client_reference_id": str(tenant.id),
        "metadata": tenant_meta,
        "subscription_data": subscription_data,
        # Launch-promo-code support; harmless when none exist.
        "allow_promotion_codes": True,
        # Czech market compliance: DPH 21 % for domestic customers,
        # reverse-charge (0 %) for EU B2B with a valid DIČ. Stripe Tax
        # computes both automatically — we just have to enable it
        # and collect the tax-id + billing address.
        "automatic_tax": {"enabled": True},
        "tax_id_collection": {"enabled": True},
        "billing_address_collection": "required",
        # Render the Stripe-hosted Checkout in Czech.
        "locale": "cs",
    }
    # Re-use an existing Stripe Customer when the tenant already has
    # one (avoids duplicate customers on repeated checkouts); fall back
    # to customer_email on the first checkout.
    existing_customer = getattr(tenant, "stripe_customer_id", None)
    if existing_customer:
        session_kwargs["customer"] = existing_customer
        # ``customer_update`` is only valid (and required) when a
        # ``customer`` is supplied alongside ``automatic_tax``. Stripe
        # refuses the session otherwise. ``shipping: auto`` is a
        # forward-compat no-op today (we never enable
        # ``shipping_address_collection``) but becomes required if the
        # supplier starts shipping physical goods to customers.
        session_kwargs["customer_update"] = {
            "address": "auto",
            "name": "auto",
            "shipping": "auto",
        }
    else:
        session_kwargs["customer_email"] = customer_email

    # Stripe idempotency: retrying within 24 h with the same key returns
    # the original session instead of creating a duplicate. Round-3
    # audit P1-#2 hardens the round-2 fix:
    #   - ``astimezone(UTC).isoformat(timespec="seconds")`` stabilises
    #     naive-vs-aware datetime drift (some test engines and SQLA
    #     round-trips strip tzinfo; the isoformat shape would otherwise
    #     flip between ``…+00:00`` and the naive form).
    #   - when the trial has been consumed (``trial_ends_at`` missing
    #     or in the past), we anchor on the local subscription id so
    #     legitimate repeated upgrade attempts get distinct keys.
    now = datetime.now(UTC)
    if trial_ends_at is not None and trial_ends_at > now:
        stable = trial_ends_at.astimezone(UTC).isoformat(timespec="seconds")
    elif subscription_id is not None:
        stable = f"sub-{subscription_id}"
    else:
        stable = "no-trial"
    idem_key = f"checkout:{tenant.id}:{plan.code}:{stable}"
    session = stripe.checkout.Session.create(**session_kwargs, idempotency_key=idem_key)
    return session.url  # type: ignore[attr-defined,no-any-return]


def create_billing_portal_session(
    settings: Settings,
    *,
    stripe_customer_id: str,
    return_url: str,
) -> str:
    """Stripe Customer Portal for upgrade/downgrade/cancel/payment method."""
    stripe = _get_stripe(settings)
    if stripe is None:
        return return_url
    idem_key = f"portal:{stripe_customer_id}"
    session = stripe.billing_portal.Session.create(
        customer=stripe_customer_id,
        return_url=return_url,
        idempotency_key=idem_key,
    )
    return session.url  # type: ignore[attr-defined,no-any-return]


# --------------------------------------------------------- webhook handling


# Stripe's default replay window is 300 s. We pin it explicitly so a
# silent SDK change cannot lengthen the attack window without review.
_WEBHOOK_TOLERANCE_SECONDS = 300


def verify_webhook(settings: Settings, payload: bytes, sig_header: str) -> Any:
    """Validate and decode a Stripe webhook payload.

    Raises :class:`BillingError` on failure, discriminating between
    signature-verification mismatches (possible attack / misconfig) and
    plain JSON-parse errors so observability / alerts can differ.
    """
    stripe = _get_stripe(settings)
    if stripe is None:
        raise BillingError("Stripe is not configured")
    try:
        return stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=settings.stripe_webhook_secret,
            tolerance=_WEBHOOK_TOLERANCE_SECONDS,
        )
    except stripe.error.SignatureVerificationError as exc:
        raise BillingError(f"Invalid webhook signature: {exc}") from exc
    except ValueError as exc:
        # Malformed JSON; Stripe SDK raises ValueError before signature
        # verification runs.
        raise BillingError(f"Malformed webhook payload: {exc}") from exc


async def record_paid_invoice(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    stripe_invoice_id: str,
    number: str | None,
    amount_cents: int,
    currency: str,
    hosted_invoice_url: str | None = None,
    pdf_url: str | None = None,
) -> Invoice:
    """Idempotent upsert invoked from the ``invoice.paid`` webhook."""
    existing = (
        await db.execute(select(Invoice).where(Invoice.stripe_invoice_id == stripe_invoice_id))
    ).scalar_one_or_none()
    if existing is not None:
        existing.status = "paid"
        existing.paid_at = datetime.now(UTC)
        await db.flush()
        return existing

    invoice = Invoice(
        tenant_id=tenant_id,
        stripe_invoice_id=stripe_invoice_id,
        number=number,
        amount_cents=amount_cents,
        currency=currency,
        status="paid",
        paid_at=datetime.now(UTC),
        hosted_invoice_url=hosted_invoice_url,
        pdf_url=pdf_url,
    )
    db.add(invoice)
    await db.flush()
    return invoice


async def list_invoices_for_tenant(db: AsyncSession, tenant_id: UUID) -> list[Invoice]:
    result = await db.execute(
        select(Invoice).where(Invoice.tenant_id == tenant_id).order_by(Invoice.created_at.desc())
    )
    return list(result.scalars().all())
