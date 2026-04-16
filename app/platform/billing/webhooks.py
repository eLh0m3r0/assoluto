"""Stripe webhook event handlers.

Each handler is a pure async function taking ``(db, event)`` and
idempotently updating local DB state. The routing happens in
:func:`dispatch_webhook` which the billing router calls after verifying
the signature + deduping on ``event.id``.

Events we care about (see Stripe docs
https://docs.stripe.com/api/events/types):

* ``checkout.session.completed`` — first confirmed subscription after
  a ``create_checkout_session`` redirect. Persists the Stripe
  customer + subscription ids onto our tenant / subscription rows.
* ``customer.subscription.created`` — backup for the above; often
  fires alongside.
* ``customer.subscription.updated`` — plan swap (via Customer Portal)
  or ``cancel_at_period_end`` toggle; syncs plan_id + status +
  period boundaries.
* ``customer.subscription.deleted`` — subscription actually ended;
  downgrades the tenant to the community plan.
* ``invoice.paid`` — cache the paid invoice for in-app history.
* ``invoice.payment_failed`` — mark subscription past_due; the
  tenant keeps access during Stripe's built-in retry window and is
  eventually downgraded via ``.deleted`` if retries fail.
* ``customer.subscription.trial_will_end`` — Stripe fires this 3
  days before the trial ends; we can hook an email (not yet wired).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging import get_logger
from app.models.tenant import Tenant
from app.platform.billing.models import Plan, Subscription
from app.platform.billing.service import record_paid_invoice

log = get_logger("app.platform.billing.webhooks")


# ----------------------------------------------------------- helpers


async def _resolve_tenant_id(db: AsyncSession, event_data: dict) -> UUID | None:
    """Find our tenant for an event data object.

    Tries three sources in order:
      1. ``metadata.tenant_id`` on the object itself (Session, Subscription)
      2. ``subscription_details.metadata.tenant_id`` (Invoice, as of
          Stripe's 2023-10-16 API)
      3. ``client_reference_id`` (Checkout Session only)
      4. Lookup ``Tenant`` by ``stripe_customer_id`` matching
          ``event_data.customer``

    Returns ``None`` when the event doesn't belong to any known tenant
    (which shouldn't happen in production but keeps handlers safe).
    """
    # 1 + 2: metadata paths
    metadata = event_data.get("metadata") or {}
    if isinstance(metadata, dict) and metadata.get("tenant_id"):
        try:
            return UUID(metadata["tenant_id"])
        except (ValueError, TypeError):
            pass

    sub_details = event_data.get("subscription_details") or {}
    sub_meta = sub_details.get("metadata") or {}
    if isinstance(sub_meta, dict) and sub_meta.get("tenant_id"):
        try:
            return UUID(sub_meta["tenant_id"])
        except (ValueError, TypeError):
            pass

    # 3: client_reference_id (Checkout Session)
    cri = event_data.get("client_reference_id")
    if cri:
        try:
            return UUID(str(cri))
        except (ValueError, TypeError):
            pass

    # 4: customer → Tenant.stripe_customer_id lookup
    customer_id = event_data.get("customer")
    if customer_id:
        t = (
            await db.execute(select(Tenant).where(Tenant.stripe_customer_id == str(customer_id)))
        ).scalar_one_or_none()
        if t is not None:
            return t.id

    return None


def _utc_from_ts(ts: Any) -> datetime | None:
    """Stripe timestamps are Unix seconds. Convert to aware datetime."""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC)
    except (TypeError, ValueError):
        return None


async def _get_subscription(db: AsyncSession, tenant_id: UUID) -> Subscription | None:
    return (
        await db.execute(select(Subscription).where(Subscription.tenant_id == tenant_id))
    ).scalar_one_or_none()


async def _get_plan_by_stripe_price(db: AsyncSession, stripe_price_id: str) -> Plan | None:
    return (
        await db.execute(select(Plan).where(Plan.stripe_price_id == stripe_price_id))
    ).scalar_one_or_none()


# ----------------------------------------------------------- handlers


async def handle_checkout_completed(db: AsyncSession, event: dict) -> None:
    """Store the Stripe customer + subscription ids on our tenant+sub row."""
    data = event.get("data", {}).get("object", {})
    tenant_id = await _resolve_tenant_id(db, data)
    if tenant_id is None:
        log.warning("stripe.webhook.no_tenant", event_type=event.get("type"))
        return

    customer_id = data.get("customer")
    subscription_id = data.get("subscription")

    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if tenant is None:
        log.warning("stripe.webhook.tenant_missing", tenant_id=str(tenant_id))
        return

    if customer_id and tenant.stripe_customer_id != customer_id:
        tenant.stripe_customer_id = customer_id

    subscription = await _get_subscription(db, tenant_id)
    if subscription is not None and subscription_id:
        subscription.stripe_customer_id = customer_id
        subscription.stripe_subscription_id = subscription_id
        # Don't override status here — the subscription.created/updated
        # handlers own that. But the minimum is to clear any "demo".
        if subscription.status == "demo":
            subscription.status = "trialing"

    await db.flush()


async def handle_subscription_upserted(db: AsyncSession, event: dict) -> None:
    """Handle created / updated: sync status, period, plan, cancel flag."""
    data = event.get("data", {}).get("object", {})
    tenant_id = await _resolve_tenant_id(db, data)
    if tenant_id is None:
        log.warning("stripe.webhook.no_tenant", event_type=event.get("type"))
        return

    subscription = await _get_subscription(db, tenant_id)
    if subscription is None:
        log.warning("stripe.webhook.subscription_missing", tenant_id=str(tenant_id))
        return

    subscription.stripe_subscription_id = data.get("id") or subscription.stripe_subscription_id
    subscription.stripe_customer_id = data.get("customer") or subscription.stripe_customer_id
    new_status = data.get("status")
    if new_status:
        subscription.status = new_status

    subscription.current_period_start = (
        _utc_from_ts(data.get("current_period_start")) or subscription.current_period_start
    )
    subscription.current_period_end = (
        _utc_from_ts(data.get("current_period_end")) or subscription.current_period_end
    )
    subscription.trial_ends_at = _utc_from_ts(data.get("trial_end")) or subscription.trial_ends_at
    subscription.cancel_at_period_end = bool(data.get("cancel_at_period_end", False))

    # Plan swap: the first line item's price.id maps to one of our Plan rows.
    items = data.get("items", {}).get("data", []) or []
    if items:
        price = (items[0] or {}).get("price", {}) or {}
        price_id = price.get("id")
        if price_id:
            plan = await _get_plan_by_stripe_price(db, price_id)
            if plan is not None:
                subscription.plan_id = plan.id

    await db.flush()


async def handle_subscription_deleted(db: AsyncSession, event: dict) -> None:
    """Subscription actually ended — downgrade to community plan."""
    data = event.get("data", {}).get("object", {})
    tenant_id = await _resolve_tenant_id(db, data)
    if tenant_id is None:
        return

    subscription = await _get_subscription(db, tenant_id)
    if subscription is None:
        return

    community = (
        await db.execute(select(Plan).where(Plan.code == "community"))
    ).scalar_one_or_none()
    if community is not None:
        subscription.plan_id = community.id
    subscription.status = "canceled"
    subscription.cancel_at_period_end = False
    await db.flush()


async def handle_invoice_paid(db: AsyncSession, event: dict) -> None:
    """Cache the paid invoice locally for the in-app history."""
    data = event.get("data", {}).get("object", {})
    tenant_id = await _resolve_tenant_id(db, data)
    if tenant_id is None:
        log.warning("stripe.webhook.invoice_no_tenant", invoice_id=data.get("id"))
        return

    await record_paid_invoice(
        db,
        tenant_id=tenant_id,
        stripe_invoice_id=str(data.get("id", "")),
        number=data.get("number"),
        amount_cents=int(data.get("amount_paid", 0)),
        currency=str(data.get("currency", "czk")).upper()[:3],
        hosted_invoice_url=data.get("hosted_invoice_url"),
        pdf_url=data.get("invoice_pdf"),
    )


async def handle_invoice_payment_failed(db: AsyncSession, event: dict) -> None:
    """Flag the subscription past_due; Stripe's Smart Retries take it from here."""
    data = event.get("data", {}).get("object", {})
    tenant_id = await _resolve_tenant_id(db, data)
    if tenant_id is None:
        return

    subscription = await _get_subscription(db, tenant_id)
    if subscription is None:
        return
    subscription.status = "past_due"
    await db.flush()


async def handle_trial_will_end(db: AsyncSession, event: dict) -> None:
    """Placeholder: Stripe fires this 3 days before trial end.

    A follow-up change will wire an email reminder here (R5). For now we
    just log so the event is observable via structured logs.
    """
    data = event.get("data", {}).get("object", {})
    log.info(
        "stripe.webhook.trial_will_end",
        subscription_id=data.get("id"),
        trial_end=data.get("trial_end"),
    )


# ----------------------------------------------------------- dispatcher


HANDLERS: dict[str, Any] = {
    "checkout.session.completed": handle_checkout_completed,
    "customer.subscription.created": handle_subscription_upserted,
    "customer.subscription.updated": handle_subscription_upserted,
    "customer.subscription.deleted": handle_subscription_deleted,
    "invoice.paid": handle_invoice_paid,
    "invoice.payment_failed": handle_invoice_payment_failed,
    "customer.subscription.trial_will_end": handle_trial_will_end,
}


async def dispatch_webhook(db: AsyncSession, event: dict) -> None:
    """Route to the right handler based on ``event.type``. Unknown = no-op."""
    event_type = event.get("type", "")
    handler = HANDLERS.get(event_type)
    if handler is None:
        log.info("stripe.webhook.ignored", event_type=event_type)
        return
    await handler(db, event)
