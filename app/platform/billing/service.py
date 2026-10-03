"""Billing service — demo + live mode helpers.

All Stripe API calls are funnelled through ``_get_stripe()`` which lazily
imports the ``stripe`` package and configures the API key. In demo mode
(no ``STRIPE_SECRET_KEY`` set) ``_get_stripe()`` returns ``None`` and the
caller falls back to local-only bookkeeping.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

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

# Plans that are shown on the dashboard but can never be bought through
# a self-service checkout. Enterprise is "price on request" (stored as
# 0 Kč); a hand-made POST to /platform/billing/checkout/enterprise used
# to grant unlimited caps for free in demo mode (BIZ-01).
NON_CHECKOUT_PLAN_CODES: frozenset[str] = HIDDEN_PLAN_CODES | frozenset({"enterprise"})

# --------------------------------------------------- entitlement rules
#
# One table for every subscription status we can hold, so "does this
# tenant still get access?" has exactly one answer (Codex-8). The
# periodic job ``enforce_canceled_subscriptions`` applies the cut-off;
# the grace constants live next to it in ``app.tasks.periodic`` (core
# may not import this package).
#
#   status               access                    cut-off anchor + grace
#   -------------------  ------------------------  ---------------------------------
#   trialing / demo      full                      trial_ends_at → canceled (expiry job)
#   active (Stripe)      full                      — (Stripe drives transitions)
#   active (manual)      full                      current_period_end → canceled
#   past_due             full during dunning       status_changed_at + PAST_DUE_GRACE_DAYS
#   incomplete           full (first payment SCA)  status_changed_at + NO_ACCESS_GRACE_DAYS
#   unpaid               none after grace          status_changed_at + NO_ACCESS_GRACE_DAYS
#   incomplete_expired   none after grace          status_changed_at + NO_ACCESS_GRACE_DAYS
#   paused               none after grace          status_changed_at + NO_ACCESS_GRACE_DAYS
#   canceled             none after grace          current_period_end + CANCEL_GRACE_DAYS
#
# Stripe statuses that still own a live, modifiable subscription — a
# plan change goes through ``Subscription.modify`` rather than a new
# Checkout (Codex-1).
MODIFIABLE_STRIPE_STATUSES: frozenset[str] = frozenset({"active", "trialing", "past_due"})
# Stripe statuses where the subscription exists but needs a payment
# fix first; neither a new checkout nor a plan change is safe.
BLOCKED_STRIPE_STATUSES: frozenset[str] = frozenset({"unpaid", "incomplete", "paused"})

# Display order for the billing-dashboard plan grid. Sorting purely by
# ``monthly_price_cents`` puts Enterprise (price-on-request, stored as 0)
# first — visually it then reads as the cheapest, which is the opposite
# of what the operator expects. This explicit sequence pins the plans
# in the marketing order: Starter → Pro → Enterprise (= rightmost = most
# expensive). Unknown plan codes append at the end so a future plan
# stays visible even before it gets added here.
PLAN_DISPLAY_ORDER: tuple[str, ...] = ("starter", "pro", "enterprise")


def _plan_sort_key(plan: Plan) -> tuple[int, str]:
    try:
        return (PLAN_DISPLAY_ORDER.index(plan.code), plan.code)
    except ValueError:
        return (len(PLAN_DISPLAY_ORDER), plan.code)


async def list_plans(db: AsyncSession) -> list[Plan]:
    """Active plans visible to hosted tenants — community deliberately
    excluded (see HIDDEN_PLAN_CODES). Sorted via :data:`PLAN_DISPLAY_ORDER`
    so Enterprise (price-on-request) renders rightmost rather than first.
    """
    result = await db.execute(
        select(Plan).where(Plan.is_active.is_(True)).where(Plan.code.notin_(HIDDEN_PLAN_CODES))
    )
    return sorted(result.scalars().all(), key=_plan_sort_key)


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
      ``canceled``. ``current_period_end`` is kept when it is still in
      the future (paid/trial time is honoured), otherwise it is set to
      ``now()`` so the grace starts at the cancellation.
    * Live mode with a Stripe sub: ``stripe.Subscription.modify(
      cancel_at_period_end=True)`` is called. The user keeps full access
      until Stripe's natural period end. The
      ``customer.subscription.deleted`` webhook then sets
      ``status='canceled'`` locally and the periodic job takes over from
      there.

    **Idempotent, and never extends access (Codex-7).** A subscription
    that is already canceled (or already scheduled to cancel) is left
    untouched and ``("already", access_ends_at)`` is returned — posting
    the cancel form every day used to push ``current_period_end`` to
    "now" each time and keep the 3-day grace window permanently ahead
    of the enforcement job.

    Returns ``("flipped", access_ends_at)`` for the immediate-flip path,
    ``("scheduled", stripe_period_end)`` for the Stripe-cancel path or
    ``("already", access_ends_at)`` for a repeated cancel.

    ``actor_label`` is recorded on the tenant's ``audit_events`` row
    (action ``billing.subscription_canceled``) so a tenant admin can
    see who cancelled their plan. The audit row is written with an
    explicit ``tenant_id`` because this service runs on the platform
    DB session which bypasses RLS — there's no ``app.tenant_id``
    setting to read from.
    """
    from app.services import audit_service
    from app.services.audit_service import ActorInfo

    now = datetime.now(UTC)
    if subscription.status == "canceled":
        anchor = subscription.current_period_end or subscription.canceled_at or now
        return ("already", anchor + timedelta(days=CANCEL_GRACE_DAYS))

    stripe = _get_stripe(settings)
    stripe_sub_id = getattr(subscription, "stripe_subscription_id", None)

    if stripe is None or not stripe_sub_id:
        # Demo mode OR live without a Stripe sub (trial that never
        # converted). Flip locally and start the grace clock.
        status_before = subscription.status
        subscription.status = "canceled"
        subscription.cancel_at_period_end = False
        subscription.canceled_at = subscription.canceled_at or now
        # Keep period_end when it is still in the future (paid or trial
        # time the tenant already has); otherwise the grace starts now.
        # Only reachable once — the early return above makes a second
        # cancel a no-op, so this can never be used to roll the window.
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

    if subscription.cancel_at_period_end:
        # Already scheduled with Stripe — nothing to do, and calling
        # Stripe again would only produce a duplicate audit row.
        return ("already", subscription.current_period_end)

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
    subscription.canceled_at = subscription.canceled_at or now
    await db.flush()
    period_end_dt = _utc_or_none(stripe_period_value(stripe_sub, "current_period_end"))
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


def stripe_get(obj: Any, key: str) -> Any:
    """Read ``key`` from a Stripe object or a plain dict (webhooks, mocks).

    ``StripeObject`` supports ``[]``, but its attribute namespace clashes
    with dict methods (``obj.items`` is the dict method, not the
    subscription's line items) — this helper covers both shapes safely.
    """
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    try:
        return obj[key]
    except (KeyError, TypeError, IndexError):
        value = getattr(obj, key, None)
        return None if callable(value) else value


def stripe_period_value(stripe_sub: Any, key: str) -> Any:
    """``current_period_start`` / ``current_period_end`` of a subscription.

    Stripe API versions from 2025-03-31 ("basil") moved the billing
    period from the Subscription onto each subscription *item*. Read the
    top level first (older API versions, our test fixtures) and fall
    back to the first item that carries it.
    """
    value = stripe_get(stripe_sub, key)
    if value:
        return value
    items = stripe_get(stripe_get(stripe_sub, "items"), "data") or []
    for item in items:
        value = stripe_get(item, key)
        if value:
            return value
    return None


def _utc_or_none(ts: Any) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC)
    except (TypeError, ValueError):
        return None


async def change_subscription_plan(
    db: AsyncSession,
    settings: Settings,
    *,
    subscription: Subscription,
    plan: Plan,
    actor_label: str | None = None,
) -> None:
    """Swap the plan on the tenant's EXISTING Stripe subscription.

    Codex-1 / LOGIC-6 / BIZ-03: plan changes used to open a fresh
    Checkout session, i.e. a *second* recurring subscription, so the
    first live upgrade would have billed the customer twice a month.
    Now the price item of the one subscription is swapped in place with
    ``proration_behavior="create_prorations"`` — Stripe credits the
    unused part of the old price and charges the new one pro rata on
    the next invoice. Downgrades take effect immediately with the same
    credit (a scheduled end-of-period downgrade needs a subscription
    schedule and is deliberately not done here).

    A pending ``cancel_at_period_end`` is cleared: picking a plan is an
    explicit "I want to keep going".

    The local ``plan_id`` is updated optimistically; the
    ``customer.subscription.updated`` webhook that follows confirms it.
    """
    from app.services import audit_service
    from app.services.audit_service import ActorInfo

    stripe = _get_stripe(settings)
    stripe_sub_id = subscription.stripe_subscription_id
    if stripe is None or not stripe_sub_id:
        raise BillingError("no Stripe subscription to modify")
    if not plan.stripe_price_id:
        raise BillingError(f"Plan '{plan.code}' has no stripe_price_id configured")

    try:
        stripe_sub = stripe.Subscription.retrieve(stripe_sub_id)
    except Exception as exc:
        raise BillingError(f"Stripe retrieve failed: {exc}") from exc

    items = stripe_get(stripe_get(stripe_sub, "items"), "data") or []
    if not items:
        raise BillingError("Stripe subscription has no items")
    # Prefer the item whose price is one of OUR plan prices: a setup fee
    # or add-on line item must not be the one we swap (same rule as the
    # webhook's plan detection).
    known_prices = {
        price_id
        for (price_id,) in (
            await db.execute(select(Plan.stripe_price_id).where(Plan.stripe_price_id.is_not(None)))
        ).all()
    }
    target_item = next(
        (it for it in items if stripe_get(stripe_get(it, "price"), "id") in known_prices),
        items[0],
    )
    old_price = stripe_get(stripe_get(target_item, "price"), "id")

    try:
        stripe.Subscription.modify(
            stripe_sub_id,
            items=[{"id": stripe_get(target_item, "id"), "price": plan.stripe_price_id}],
            proration_behavior="create_prorations",
            cancel_at_period_end=False,
            # Unique per click. The per-tenant lock in the router plus the
            # "already on this plan" check stop double submits; a
            # deterministic key would make Stripe replay a cached response
            # (= silently not switch) when a customer goes A → B → A
            # within 24 h.
            idempotency_key=f"plan-change:{stripe_sub_id}:{plan.code}:{uuid4()}",
        )
    except Exception as exc:
        raise BillingError(f"Stripe plan change failed: {exc}") from exc

    plan_before = subscription.plan_id
    subscription.plan_id = plan.id
    subscription.cancel_at_period_end = False
    subscription.canceled_at = None
    await db.flush()
    await audit_service.record(
        db,
        action="billing.plan_changed",
        entity_type="subscription",
        entity_id=subscription.id,
        entity_label=f"plan={plan.code} stripe_sub={stripe_sub_id}",
        actor=ActorInfo(type="user", id=None, label=actor_label or "platform-identity"),
        before={"plan_id": str(plan_before), "stripe_price": old_price},
        after={"plan_id": str(plan.id), "stripe_price": plan.stripe_price_id},
        tenant_id=subscription.tenant_id,
    )


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
    never_trialed: bool = False,
    previous_session_id: str | None = None,
) -> str:
    """Return a URL the caller should redirect to (see
    :func:`_create_checkout_session_obj` for the parameters)."""
    session = _create_checkout_session_obj(
        settings,
        tenant=tenant,
        plan=plan,
        success_url=success_url,
        cancel_url=cancel_url,
        customer_email=customer_email,
        trial_ends_at=trial_ends_at,
        subscription_id=subscription_id,
        never_trialed=never_trialed,
        previous_session_id=previous_session_id,
    )
    return success_url if session is None else session.url


def _create_checkout_session_obj(
    settings: Settings,
    *,
    tenant: Tenant,
    plan: Plan,
    success_url: str,
    cancel_url: str,
    customer_email: str,
    trial_ends_at: datetime | None = None,
    subscription_id: UUID | None = None,
    never_trialed: bool = False,
    previous_session_id: str | None = None,
) -> Any | None:
    """Create a Stripe Checkout session; ``None`` in demo mode.

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

    ``never_trialed`` — the tenant has never had a trial (no local
    subscription row at all). Only then does a checkout without
    ``trial_ends_at`` get a fresh ``trial_period_days`` window; a
    manually-billed tenant whose ``trial_ends_at`` was cleared used to
    receive a brand-new 30-day Stripe trial (LOGIC-18).

    ``previous_session_id`` — the tenant's previous Checkout session,
    already expired by :func:`open_checkout_session`. Part of the
    idempotency key so the replacement session is a *new* session and
    not Stripe's cached copy of the expired one.
    """
    stripe = _get_stripe(settings)
    if stripe is None:
        # Demo mode (no Stripe configured): the caller decides whether a
        # local plan switch is allowed (never in production — D2).
        return None
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
    elif trial_ends_at is None and never_trialed:
        subscription_data["trial_period_days"] = TRIAL_DAYS
    # else: trial already consumed (or the tenant is manually billed and
    # its trial was cleared) — no trial on the new checkout.

    # Single source of truth for "does this operator charge VAT?", shared
    # with invoice_pdf_service. Empty DIČ = neplátce DPH.
    operator_is_vat_registered = bool((settings.platform_operator_dic or "").strip())

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
        # Tax is driven by ONE setting: PLATFORM_OPERATOR_DIC.
        #
        # The operator is currently a non-VAT payer (§6 ZDPH, stated on
        # /imprint), so no DPH may be charged and no daňový doklad may
        # be issued. These flags were hard-coded True, which told Stripe
        # Tax to add 21 % on top of the listed price — money the
        # operator is not registered to collect, on an invoice they
        # cannot legally issue. Asking for a DIČ was equally pointless:
        # reverse-charge only exists for a VAT-registered supplier.
        #
        # invoice_pdf_service already keys its whole VAT/non-VAT layout
        # off the same setting (``supplier_is_vat``), so registering for
        # VAT later is one env var and Stripe, the PDF and the document
        # label all flip together.
        "automatic_tax": {"enabled": operator_is_vat_registered},
        "tax_id_collection": {"enabled": operator_is_vat_registered},
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
    if previous_session_id:
        idem_key = f"{idem_key}:after-{previous_session_id}"
    return stripe.checkout.Session.create(**session_kwargs, idempotency_key=idem_key)


class CheckoutInProgress(BillingError):
    """A previous Checkout session completed and its webhook has not been
    processed yet — opening another one could create a second paid
    subscription."""


async def open_checkout_session(
    db: AsyncSession,
    settings: Settings,
    *,
    tenant: Tenant,
    subscription: Subscription | None,
    plan: Plan,
    success_url: str,
    cancel_url: str,
    customer_email: str,
) -> str:
    """Live-mode checkout with **one open session per tenant** (Codex-1).

    The caller holds the per-tenant billing lock. Before creating a new
    session we look at the one we created last time:

    * still ``open`` for the same plan → reuse it (back-button / double
      click lands on the same payment page);
    * ``open`` for another plan → expire it, so the customer cannot pay
      both and end up with two subscriptions;
    * ``complete`` but its subscription is not ours yet → the webhook is
      still in flight; refuse with :class:`CheckoutInProgress`.

    The new session id is stored on the subscription row and cleared by
    the ``checkout.session.completed`` webhook.
    """
    stripe = _get_stripe(settings)
    if stripe is None:
        raise BillingError("Stripe is not configured")

    previous = subscription.pending_checkout_session_id if subscription else None
    if previous:
        try:
            prev_session = stripe.checkout.Session.retrieve(previous)
        except Exception:
            prev_session = None  # unknown/deleted — treat as gone
        prev_status = stripe_get(prev_session, "status")
        if prev_status == "open":
            prev_plan = stripe_get(stripe_get(prev_session, "metadata"), "plan_code")
            if prev_plan == plan.code and stripe_get(prev_session, "url"):
                return str(stripe_get(prev_session, "url"))
            try:
                stripe.checkout.Session.expire(previous)
            except Exception as exc:
                # Expiring fails only if it just completed — same as below.
                raise CheckoutInProgress(f"could not expire session {previous}: {exc}") from exc
        elif prev_status == "complete":
            prev_sub = stripe_get(prev_session, "subscription")
            if (
                prev_sub
                and subscription is not None
                and (prev_sub != subscription.stripe_subscription_id)
            ):
                raise CheckoutInProgress(f"session {previous} completed, webhook pending")

    session = _create_checkout_session_obj(
        settings,
        tenant=tenant,
        plan=plan,
        success_url=success_url,
        cancel_url=cancel_url,
        customer_email=customer_email,
        trial_ends_at=subscription.trial_ends_at if subscription else None,
        subscription_id=subscription.id if subscription else None,
        never_trialed=subscription is None,
        previous_session_id=previous,
    )
    assert session is not None  # live mode
    if subscription is not None:
        subscription.pending_checkout_session_id = stripe_get(session, "id")
        await db.flush()
    return str(stripe_get(session, "url"))


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
    return session.url


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
    if not (settings.stripe_webhook_secret or "").strip():
        # SEC-1: the Stripe SDK happily verifies an HMAC keyed with "" —
        # anyone could then forge checkout/subscription events. Refuse
        # outright; ``create_app`` also refuses to boot in production
        # with a secret key but no webhook secret.
        raise BillingError("Stripe webhook secret is not configured")
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


# Invoice statuses produced by refunds — "paid" must never overwrite them,
# and "refunded" must never fall back to "partially_refunded".
REFUND_STATUSES: frozenset[str] = frozenset({"refunded", "partially_refunded"})


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
    """Idempotent upsert invoked from the ``invoice.paid`` webhook.

    Never erases a refund (Codex-12): a ``charge.refunded`` can arrive
    before ``invoice.paid`` (it then leaves a placeholder row, see
    ``handle_charge_refunded``) or a re-delivered ``invoice.paid`` can
    arrive after the refund. Either way the refund status wins; this
    call only fills in the invoice details.
    """
    existing = (
        await db.execute(select(Invoice).where(Invoice.stripe_invoice_id == stripe_invoice_id))
    ).scalar_one_or_none()
    if existing is not None:
        if existing.status not in REFUND_STATUSES:
            existing.status = "paid"
        existing.paid_at = existing.paid_at or datetime.now(UTC)
        existing.number = existing.number or number
        if amount_cents:
            existing.amount_cents = amount_cents
            existing.currency = currency
        existing.hosted_invoice_url = existing.hosted_invoice_url or hosted_invoice_url
        existing.pdf_url = existing.pdf_url or pdf_url
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
