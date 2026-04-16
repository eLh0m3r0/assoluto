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

import contextlib
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


class WebhookNotYetReady(Exception):
    """Raised by a handler when the event can be processed later but
    not now — e.g. a ``customer.subscription.updated`` arrived for a
    tenant that somehow has no local ``Subscription`` row yet, or a
    ``checkout.session.completed`` event has an unresolvable tenant.

    The webhook router catches this, rolls back the dedup INSERT, and
    returns 503 — Stripe's delivery layer will retry with backoff.
    Critically this is how we avoid SILENTLY committing the dedup row
    (and never getting another delivery) after a no-op handler return.
    """


# ----------------------------------------------------------- helpers


async def _resolve_tenant_id(db: AsyncSession, event_data: dict) -> UUID | None:
    """Find our tenant for an event data object.

    **Security note (2nd-round audit fix).**
    Stripe metadata on a Customer, Subscription, or Invoice is editable
    by the end customer via the Stripe Customer Portal or API; a paying
    tenant A could put tenant B's UUID in their own object's metadata
    and thereby hijack webhook effects (downgrade / past-due / misattr
    invoices). To close this, we now resolve in the order:

      1. ``event_data.customer`` → ``Tenant.stripe_customer_id`` lookup
         (authoritative — only we write it, via ``checkout.session.completed``)
      2. ``client_reference_id`` (only on Checkout Session; we set it
         server-side with the user's own tenant_id)
      3. ``metadata.tenant_id`` / ``subscription_details.metadata.tenant_id``
         — trusted only when no Stripe customer exists yet (first
         checkout completion) AND **never** when ``event_data.customer``
         matches a tenant whose id differs from the metadata value.

    Any metadata-derived tenant_id is cross-checked against the
    ``customer``-lookup tenant when both are present; on mismatch we
    refuse to resolve (returns None, handler logs + no-op).
    """
    # 1. customer → tenant (authoritative)
    customer_id = event_data.get("customer")
    tenant_by_customer: UUID | None = None
    if customer_id:
        t = (
            await db.execute(select(Tenant).where(Tenant.stripe_customer_id == str(customer_id)))
        ).scalar_one_or_none()
        if t is not None:
            tenant_by_customer = t.id

    # 2. client_reference_id (Checkout Session only; server-minted)
    cri_uuid: UUID | None = None
    cri = event_data.get("client_reference_id")
    if cri:
        with contextlib.suppress(ValueError, TypeError):
            cri_uuid = UUID(str(cri))

    # 3. metadata (customer-writeable — lowest trust)
    metadata_uuid: UUID | None = None
    metadata = event_data.get("metadata") or {}
    if isinstance(metadata, dict) and metadata.get("tenant_id"):
        with contextlib.suppress(ValueError, TypeError):
            metadata_uuid = UUID(metadata["tenant_id"])
    if metadata_uuid is None:
        sub_details = event_data.get("subscription_details") or {}
        sub_meta = sub_details.get("metadata") or {}
        if isinstance(sub_meta, dict) and sub_meta.get("tenant_id"):
            with contextlib.suppress(ValueError, TypeError):
                metadata_uuid = UUID(sub_meta["tenant_id"])

    # Cross-check: if customer lookup produced a tenant AND metadata or
    # client_reference_id point at a DIFFERENT tenant, refuse to resolve.
    # This is the spoofing guard.
    if tenant_by_customer is not None:
        for candidate in (cri_uuid, metadata_uuid):
            if candidate is not None and candidate != tenant_by_customer:
                log.warning(
                    "stripe.webhook.tenant_spoof_blocked",
                    customer_tenant=str(tenant_by_customer),
                    claimed_tenant=str(candidate),
                    customer=str(customer_id),
                )
                return None
        return tenant_by_customer

    # No customer on file yet — fall back to server-minted cri first,
    # then metadata. This is the first checkout.session.completed path.
    if cri_uuid is not None:
        return cri_uuid
    return metadata_uuid


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
        raise WebhookNotYetReady("unresolvable tenant on checkout.session.completed")

    customer_id = data.get("customer")
    subscription_id = data.get("subscription")

    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if tenant is None:
        log.warning("stripe.webhook.tenant_missing", tenant_id=str(tenant_id))
        raise WebhookNotYetReady("tenant row missing")

    if customer_id and tenant.stripe_customer_id != customer_id:
        tenant.stripe_customer_id = customer_id

    subscription = await _get_subscription(db, tenant_id)
    if subscription is not None and subscription_id:
        subscription.stripe_customer_id = customer_id
        subscription.stripe_subscription_id = subscription_id
        # Don't override status here — the subscription.created/updated
        # handlers own that. Only reset a stale "demo" marker to
        # ``trialing`` when the subscription hasn't been synced yet
        # (i.e. no stripe_subscription_id persisted before this call).
        # Otherwise a late-arriving checkout.session.completed after
        # customer.subscription.created would clobber a valid "active"
        # status.
        if subscription.status == "demo":
            subscription.status = "trialing"

    await db.flush()


async def handle_subscription_upserted(db: AsyncSession, event: dict) -> None:
    """Handle created / updated: sync status, period, plan, cancel flag."""
    data = event.get("data", {}).get("object", {})
    tenant_id = await _resolve_tenant_id(db, data)
    if tenant_id is None:
        log.warning("stripe.webhook.no_tenant", event_type=event.get("type"))
        raise WebhookNotYetReady("unresolvable tenant on subscription event")

    subscription = await _get_subscription(db, tenant_id)
    if subscription is None:
        log.warning("stripe.webhook.subscription_missing", tenant_id=str(tenant_id))
        raise WebhookNotYetReady("subscription row not yet created")

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
        log.warning("stripe.webhook.no_tenant", event_type=event.get("type"))
        raise WebhookNotYetReady("unresolvable tenant on subscription deletion")

    subscription = await _get_subscription(db, tenant_id)
    if subscription is None:
        log.warning("stripe.webhook.subscription_missing", tenant_id=str(tenant_id))
        raise WebhookNotYetReady("subscription row missing for deletion")

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
        raise WebhookNotYetReady("unresolvable tenant on invoice.paid")

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
        log.warning("stripe.webhook.no_tenant", event_type=event.get("type"))
        raise WebhookNotYetReady("unresolvable tenant on invoice.payment_failed")

    subscription = await _get_subscription(db, tenant_id)
    if subscription is None:
        log.warning("stripe.webhook.subscription_missing", tenant_id=str(tenant_id))
        raise WebhookNotYetReady("subscription row missing")
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
