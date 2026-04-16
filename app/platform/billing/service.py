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

TRIAL_DAYS = 14


class BillingError(Exception):
    pass


class PlanNotFound(BillingError):
    pass


class SubscriptionNotFound(BillingError):
    pass


# ----------------------------------------------------------------- plans


async def get_plan_by_code(db: AsyncSession, code: str) -> Plan | None:
    return (await db.execute(select(Plan).where(Plan.code == code))).scalar_one_or_none()


async def list_plans(db: AsyncSession) -> list[Plan]:
    result = await db.execute(
        select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.monthly_price_cents)
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
    trial ends without an active Stripe subscription, the tenant is
    bumped down to the community plan (handled by a periodic job).
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
) -> str:
    """Return a URL the caller should redirect to.

    * Live mode: Stripe Checkout session URL.
    * Demo mode: a fake local URL that just bounces back to ``success_url``
      so the signup/upgrade flow is testable locally.
    """
    stripe = _get_stripe(settings)
    if stripe is None or not plan.stripe_price_id:
        # Demo: pretend the user clicked "Pay", go straight to success.
        return success_url

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
    session_kwargs: dict[str, Any] = {
        "mode": "subscription",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "line_items": [{"price": plan.stripe_price_id, "quantity": 1}],
        "client_reference_id": str(tenant.id),
        "metadata": tenant_meta,
        "subscription_data": {
            "trial_period_days": TRIAL_DAYS,
            "metadata": tenant_meta,
        },
    }
    # Re-use an existing Stripe Customer when the tenant already has
    # one (avoids duplicate customers on repeated checkouts); fall back
    # to customer_email on the first checkout.
    existing_customer = getattr(tenant, "stripe_customer_id", None)
    if existing_customer:
        session_kwargs["customer"] = existing_customer
    else:
        session_kwargs["customer_email"] = customer_email

    session = stripe.checkout.Session.create(**session_kwargs)
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
    session = stripe.billing_portal.Session.create(
        customer=stripe_customer_id,
        return_url=return_url,
    )
    return session.url  # type: ignore[attr-defined,no-any-return]


# --------------------------------------------------------- webhook handling


def verify_webhook(settings: Settings, payload: bytes, sig_header: str) -> Any:
    """Validate and decode a Stripe webhook payload.

    Raises :class:`BillingError` on failure.
    """
    stripe = _get_stripe(settings)
    if stripe is None:
        raise BillingError("Stripe is not configured")
    try:
        return stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=settings.stripe_webhook_secret,
        )
    except Exception as exc:
        raise BillingError(f"Invalid webhook: {exc}") from exc


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
