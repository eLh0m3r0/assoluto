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
* ``customer.subscription.deleted`` — Stripe sub actually ended;
  flips local row to ``status='canceled'``, plan_id stays as a
  historical record. The periodic ``enforce_canceled_subscriptions``
  job hard-cuts the tenant ``CANCEL_GRACE_DAYS`` past period_end.
* ``invoice.paid`` — cache the paid invoice for in-app history.
* ``invoice.payment_failed`` — mark subscription past_due; the
  tenant keeps access during Stripe's built-in retry window and is
  eventually canceled via ``.deleted`` if retries fail.
* ``customer.subscription.trial_will_end`` — Stripe fires this 3
  days before the trial ends; syncs ``trial_ends_at`` (the reminder
  mail itself comes from the periodic trial-nurture job).
* ``charge.refunded`` — mark the cached invoice refunded.

State-machine rules (audit 2026-10-03, theme T4):

* Only events about the CURRENT Stripe subscription
  (``platform_subscriptions.stripe_subscription_id``) change state; an
  older generation's events are ignored (Codex-5).
* Within one subscription, an event older than the last applied one
  (``stripe_last_event_at``) is ignored — Stripe promises delivery,
  not order.
* ``canceled`` is terminal for a given subscription; only a new
  subscription lifts it (Codex-3), and invoices never revive it
  (Codex-4).
* Payment recovery may undo a billing hard cut, never a platform-admin
  suspension (Codex-6).
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
from app.platform.billing.service import record_paid_invoice, stripe_period_value

log = get_logger("app.platform.billing.webhooks")

# Local statuses that mean "a Stripe subscription is alive and is the
# tenant's current one" — a different subscription id must not replace it.
LIVE_STATUSES: frozenset[str] = frozenset({"active", "trialing", "past_due"})


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
    """Stripe timestamps are Unix seconds. Convert to aware datetime.

    Stripe occasionally emits ``0`` to signal a cleared timestamp
    (e.g. ``trial_end`` after a trial is cancelled). Treat that as
    ``None`` rather than writing 1970-01-01 into the DB — the UI would
    surface the epoch as a "trial ends" date which is nonsensical.
    """
    if ts is None or ts == 0:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC)
    except (TypeError, ValueError):
        return None


async def _get_subscription(db: AsyncSession, tenant_id: UUID) -> Subscription | None:
    """Load the tenant's subscription row **locked for update**.

    Every handler reads, compares (generation, watermark, terminal
    status) and then writes; two deliveries for the same tenant racing
    in parallel must see each other's result, not the same stale row.
    """
    return (
        await db.execute(
            select(Subscription).where(Subscription.tenant_id == tenant_id).with_for_update()
        )
    ).scalar_one_or_none()


async def _get_plan_by_stripe_price(db: AsyncSession, stripe_price_id: str) -> Plan | None:
    return (
        await db.execute(select(Plan).where(Plan.stripe_price_id == stripe_price_id))
    ).scalar_one_or_none()


# ----------------------------------------------------------- handlers


async def handle_checkout_completed(db: AsyncSession, event: dict) -> None:
    """Store the Stripe customer + subscription ids on our tenant+sub row.

    When the session brings a subscription id we do not track yet, it
    becomes the tenant's current subscription and the local status is
    reconciled from the session itself (Codex-3): a canceled tenant who
    resubscribes used to keep ``status='canceled'`` with the new id, so
    every following ``customer.subscription.updated`` for that id hit the
    "canceled is terminal" guard and the paying customer stayed locked
    out.
    """
    data = event.get("data", {}).get("object", {})
    tenant_id = await _resolve_tenant_id(db, data)
    if tenant_id is None:
        log.warning("stripe.webhook.no_tenant", event_type=event.get("type"))
        raise WebhookNotYetReady("unresolvable tenant on checkout.session.completed")

    customer_id = data.get("customer")
    subscription_id = data.get("subscription")
    if isinstance(subscription_id, dict):  # expanded object
        subscription_id = subscription_id.get("id")

    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if tenant is None:
        log.warning("stripe.webhook.tenant_missing", tenant_id=str(tenant_id))
        raise WebhookNotYetReady("tenant row missing")

    if customer_id and tenant.stripe_customer_id != customer_id:
        # Partial UNIQUE on tenants.stripe_customer_id (migration 1005)
        # means assigning the same id to a second tenant explodes at
        # flush time. We surface it as WebhookNotYetReady so the
        # transaction rolls back and Stripe's retry doesn't spin
        # forever on a silent 500.
        from sqlalchemy.exc import IntegrityError

        tenant.stripe_customer_id = customer_id
        try:
            await db.flush()
        except IntegrityError as exc:
            log.warning(
                "stripe.webhook.customer_id_collision",
                tenant_id=str(tenant_id),
                customer=str(customer_id),
            )
            raise WebhookNotYetReady("stripe customer id collision") from exc

    subscription = await _get_subscription(db, tenant_id)
    if subscription is None:
        await db.flush()
        return

    if subscription.pending_checkout_session_id == data.get("id"):
        subscription.pending_checkout_session_id = None

    if not subscription_id:
        await db.flush()
        return

    if customer_id:
        subscription.stripe_customer_id = customer_id

    current = subscription.stripe_subscription_id
    if current == subscription_id:
        # ``customer.subscription.created/updated`` already landed for
        # this id and owns the status (round-2 S-N6) — nothing to add.
        await db.flush()
        return

    if current and subscription.status in LIVE_STATUSES:
        # The tenant already has a live subscription and a SECOND one
        # was just paid for. The serialized checkout makes this
        # impossible through our UI; if it happens anyway, keep the
        # tracked one and shout — an operator must refund/cancel the
        # duplicate in Stripe.
        log.error(
            "stripe.webhook.duplicate_subscription",
            tenant_id=str(tenant_id),
            current_subscription=current,
            new_subscription=subscription_id,
        )
        await db.flush()
        return

    # Adopt the new subscription as the current generation. Its
    # ``created`` and the event watermark are unknown from a session —
    # leave them empty so the subscription's own events (which may carry
    # an EARLIER event timestamp than this session event) still apply.
    subscription.stripe_subscription_id = subscription_id
    subscription.stripe_subscription_created_at = None
    subscription.stripe_last_event_at = None
    subscription.cancel_at_period_end = False
    subscription.canceled_at = None
    payment_status = data.get("payment_status")
    if payment_status == "paid":
        subscription.status = "active"
    elif payment_status == "no_payment_required":
        subscription.status = "trialing"
    else:
        # ``unpaid`` = asynchronous payment method still settling.
        subscription.status = "incomplete"
    await _maybe_reactivate_tenant(db, subscription)
    log.info(
        "stripe.webhook.subscription_adopted",
        tenant_id=str(tenant_id),
        subscription_id=subscription_id,
        status=subscription.status,
        previous_subscription=current,
    )
    await db.flush()


def _accepts_new_generation(subscription: Subscription, data: dict) -> bool:
    """May an event about a subscription id we do not track replace it?

    Codex-5: events about an OLD subscription (superseded generation)
    must never overwrite the current one — a late ``updated`` used to
    replace the id, plan and period of the replacement subscription.

    * nothing tracked yet → adopt;
    * the tracked subscription is still live → refuse (an old or a
      duplicate subscription);
    * the incoming one was created before the tracked one → refuse;
    * otherwise (tracked one ended, incoming is newer or its age is
      unknown) → adopt.
    """
    if subscription.stripe_subscription_id is None:
        return True
    if subscription.status in LIVE_STATUSES:
        return False
    incoming_created = _utc_from_ts(data.get("created"))
    current_created = subscription.stripe_subscription_created_at
    return not (
        incoming_created is not None
        and current_created is not None
        and incoming_created < current_created
    )


def _is_stale(subscription: Subscription, event: dict) -> bool:
    """An older event for the current subscription delivered late."""
    event_at = _utc_from_ts(event.get("created"))
    return (
        event_at is not None
        and subscription.stripe_last_event_at is not None
        and event_at < subscription.stripe_last_event_at
    )


def _advance_watermark(subscription: Subscription, event: dict) -> None:
    event_at = _utc_from_ts(event.get("created"))
    if event_at is not None and (
        subscription.stripe_last_event_at is None or event_at > subscription.stripe_last_event_at
    ):
        subscription.stripe_last_event_at = event_at


async def _maybe_reactivate_tenant(db: AsyncSession, subscription: Subscription) -> None:
    """Paying again undoes a *billing* hard cut — never an operator one.

    ``enforce_canceled_subscriptions`` sets ``tenants.is_active = false``
    once a grace window elapses; a customer who then resubscribes must
    get their portal back without a support ticket (F-08). A platform
    admin's deactivation is a different decision (abuse, contract end)
    and payment recovery must not lift it (Codex-6).
    """
    if subscription.status not in ("active", "trialing"):
        return
    if subscription.operator_suspended_at is not None:
        log.info(
            "stripe.webhook.reactivation_blocked_operator_suspension",
            tenant_id=str(subscription.tenant_id),
        )
        return
    tenant = (
        await db.execute(select(Tenant).where(Tenant.id == subscription.tenant_id))
    ).scalar_one_or_none()
    if tenant is not None and not tenant.is_active:
        tenant.is_active = True
        log.info("stripe.webhook.tenant_reactivated", tenant_id=str(subscription.tenant_id))


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

    incoming_sub_id = data.get("id")
    if incoming_sub_id and incoming_sub_id != subscription.stripe_subscription_id:
        if not _accepts_new_generation(subscription, data):
            log.warning(
                "stripe.webhook.ignored_superseded_subscription",
                tenant_id=str(tenant_id),
                incoming_subscription=incoming_sub_id,
                current_subscription=subscription.stripe_subscription_id,
                current_status=subscription.status,
            )
            return
        # New generation: forget the previous subscription's bookkeeping.
        subscription.stripe_subscription_id = incoming_sub_id
        subscription.stripe_subscription_created_at = _utc_from_ts(data.get("created"))
        subscription.stripe_last_event_at = None
        subscription.canceled_at = None
    else:
        # Stripe does not guarantee delivery ORDER, only delivery. Dedup
        # on event.id stops the same event being applied twice, but says
        # nothing about a *different*, older event arriving late.
        #
        # 'canceled' is terminal for a given Stripe subscription (Stripe
        # never revives one): an update generated before the deletion
        # but delivered after it must not write 'active' back. Only a
        # NEW subscription id (above) may lift it.
        if subscription.status == "canceled" and incoming_sub_id:
            log.info(
                "stripe.webhook.ignored_stale_update",
                tenant_id=str(tenant_id),
                subscription_id=incoming_sub_id,
            )
            return
        if _is_stale(subscription, event):
            log.info(
                "stripe.webhook.ignored_out_of_order",
                tenant_id=str(tenant_id),
                subscription_id=incoming_sub_id,
                event_id=event.get("id"),
            )
            return
        if subscription.stripe_subscription_created_at is None:
            subscription.stripe_subscription_created_at = _utc_from_ts(data.get("created"))

    subscription.stripe_customer_id = data.get("customer") or subscription.stripe_customer_id
    new_status = data.get("status")
    if new_status:
        subscription.status = new_status

    subscription.current_period_start = (
        _utc_from_ts(stripe_period_value(data, "current_period_start"))
        or subscription.current_period_start
    )
    subscription.current_period_end = (
        _utc_from_ts(stripe_period_value(data, "current_period_end"))
        or subscription.current_period_end
    )
    trial_end_ts = data.get("trial_end")
    if trial_end_ts is not None:
        subscription.trial_ends_at = _utc_from_ts(trial_end_ts)
    subscription.cancel_at_period_end = bool(data.get("cancel_at_period_end", False))
    if not subscription.cancel_at_period_end and subscription.status in LIVE_STATUSES:
        subscription.canceled_at = None  # a scheduled cancel was reverted

    # Plan swap: scan ALL line items (not just the first) for a price
    # that matches one of our seeded Plan rows. Stripe may add setup-fee
    # or one-off add-on line items alongside the recurring plan; picking
    # items[0] blindly would misdetect. Round-2 audit S-N4.
    items = data.get("items", {}).get("data", []) or []
    for item in items:
        price = (item or {}).get("price") or {}
        price_id = price.get("id")
        if not price_id:
            continue
        plan = await _get_plan_by_stripe_price(db, price_id)
        if plan is not None:
            subscription.plan_id = plan.id
            break

    _advance_watermark(subscription, event)
    await _maybe_reactivate_tenant(db, subscription)
    await db.flush()


async def handle_subscription_deleted(db: AsyncSession, event: dict) -> None:
    """Stripe subscription actually ended — mark canceled locally.

    Does NOT flip ``plan_id`` to a free tier. The row is just stamped
    ``status='canceled'``; the ``plan_id`` stays as a record of what the
    tenant had. The periodic ``enforce_canceled_subscriptions`` job then
    hard-cuts the tenant ``CANCEL_GRACE_DAYS`` after the access end.

    Only the CURRENT subscription can cancel the tenant (LOGIC-6 /
    Codex-5): a deletion of an old or duplicate subscription used to
    schedule a hard cut of a tenant still paying for its replacement.
    """
    data = event.get("data", {}).get("object", {})
    tenant_id = await _resolve_tenant_id(db, data)
    if tenant_id is None:
        log.warning("stripe.webhook.no_tenant", event_type=event.get("type"))
        raise WebhookNotYetReady("unresolvable tenant on subscription deletion")

    subscription = await _get_subscription(db, tenant_id)
    if subscription is None:
        log.warning("stripe.webhook.subscription_missing", tenant_id=str(tenant_id))
        raise WebhookNotYetReady("subscription row missing for deletion")

    incoming_sub_id = data.get("id")
    if not incoming_sub_id or incoming_sub_id != subscription.stripe_subscription_id:
        log.warning(
            "stripe.webhook.ignored_foreign_deletion",
            tenant_id=str(tenant_id),
            incoming_subscription=incoming_sub_id,
            current_subscription=subscription.stripe_subscription_id,
        )
        return

    now = datetime.now(UTC)
    subscription.status = "canceled"
    subscription.cancel_at_period_end = False
    subscription.canceled_at = (
        subscription.canceled_at or _utc_from_ts(data.get("canceled_at")) or now
    )
    # Access runs to the end of the paid period, but never past the
    # moment Stripe actually ended the subscription: an ``unpaid``
    # subscription deleted mid-period has already advanced
    # current_period_end into a period nobody paid for.
    period_end = _utc_from_ts(stripe_period_value(data, "current_period_end"))
    ended_at = _utc_from_ts(data.get("ended_at"))
    if period_end and ended_at:
        period_end = min(period_end, ended_at)
    period_end = period_end or ended_at
    if period_end is not None:
        subscription.current_period_end = period_end
    elif subscription.current_period_end is None:
        subscription.current_period_end = now
    _advance_watermark(subscription, event)
    await db.flush()


async def handle_invoice_paid(db: AsyncSession, event: dict) -> None:
    """Cache the paid invoice locally for the in-app history."""
    data = event.get("data", {}).get("object", {})
    tenant_id = await _resolve_tenant_id(db, data)
    if tenant_id is None:
        log.warning("stripe.webhook.invoice_no_tenant", invoice_id=data.get("id"))
        raise WebhookNotYetReady("unresolvable tenant on invoice.paid")

    invoice_currency = str(data.get("currency", "czk")).upper()[:3]

    # Cross-check: if the tenant has an active subscription and its plan
    # currency doesn't match this invoice currency, we log a loud warning
    # (round-2 audit S-N9). We still record the invoice so accounting
    # isn't blocked, but an operator alert is warranted.
    subscription = await _get_subscription(db, tenant_id)
    if subscription is not None:
        plan = (
            await db.execute(select(Plan).where(Plan.id == subscription.plan_id))
        ).scalar_one_or_none()
        if plan is not None and plan.currency.upper() != invoice_currency:
            log.warning(
                "stripe.webhook.currency_mismatch",
                tenant_id=str(tenant_id),
                plan_currency=plan.currency,
                invoice_currency=invoice_currency,
                invoice_id=data.get("id"),
            )

    await record_paid_invoice(
        db,
        tenant_id=tenant_id,
        stripe_invoice_id=str(data.get("id", "")),
        number=data.get("number"),
        amount_cents=int(data.get("amount_paid", 0)),
        currency=invoice_currency,
        hosted_invoice_url=data.get("hosted_invoice_url"),
        pdf_url=data.get("invoice_pdf"),
    )


def _invoice_subscription_id(data: dict) -> str | None:
    """Subscription id of an invoice, for both API shapes.

    Before API version 2025-03-31 it is ``invoice.subscription``; from
    "basil" on it lives at ``invoice.parent.subscription_details.subscription``.
    """
    sub = data.get("subscription")
    if isinstance(sub, dict):
        sub = sub.get("id")
    if sub:
        return str(sub)
    parent = data.get("parent") or {}
    details = parent.get("subscription_details") or {}
    sub = details.get("subscription")
    if isinstance(sub, dict):
        sub = sub.get("id")
    return str(sub) if sub else None


async def handle_invoice_payment_failed(db: AsyncSession, event: dict) -> None:
    """Flag the subscription past_due; Stripe's Smart Retries take it from here.

    Codex-4: the failure must concern the CURRENT subscription and may
    only move a paying state (active / trialing) to past_due. A delayed
    failure used to turn a ``canceled`` row back into ``past_due`` and
    thereby take the tenant out of the cancellation hard-cut for good.
    """
    data = event.get("data", {}).get("object", {})
    tenant_id = await _resolve_tenant_id(db, data)
    if tenant_id is None:
        log.warning("stripe.webhook.no_tenant", event_type=event.get("type"))
        raise WebhookNotYetReady("unresolvable tenant on invoice.payment_failed")

    subscription = await _get_subscription(db, tenant_id)
    if subscription is None:
        log.warning("stripe.webhook.subscription_missing", tenant_id=str(tenant_id))
        raise WebhookNotYetReady("subscription row missing")

    invoice_sub = _invoice_subscription_id(data)
    if not invoice_sub or invoice_sub != subscription.stripe_subscription_id:
        log.warning(
            "stripe.webhook.payment_failed_foreign_subscription",
            tenant_id=str(tenant_id),
            invoice_subscription=invoice_sub,
            current_subscription=subscription.stripe_subscription_id,
        )
        return
    if _is_stale(subscription, event):
        log.info("stripe.webhook.ignored_out_of_order", event_id=event.get("id"))
        return
    if subscription.status in ("active", "trialing"):
        subscription.status = "past_due"
    _advance_watermark(subscription, event)
    await db.flush()


async def handle_trial_will_end(db: AsyncSession, event: dict) -> None:
    """Stripe fires this 3 days before a *Stripe-side* trial ends.

    The reminder e-mail itself is sent by the periodic trial-nurture
    job (stage ``ending``, 5 days ahead) for every trialing tenant —
    including Stripe-linked trials — so this handler does not mail
    again (one reminder, not two). It only keeps ``trial_ends_at`` in
    sync for the current subscription so that job and the in-app
    countdown use Stripe's date.
    """
    data = event.get("data", {}).get("object", {})
    log.info(
        "stripe.webhook.trial_will_end",
        subscription_id=data.get("id"),
        trial_end=data.get("trial_end"),
    )
    tenant_id = await _resolve_tenant_id(db, data)
    if tenant_id is None:
        return
    subscription = await _get_subscription(db, tenant_id)
    if subscription is None or subscription.stripe_subscription_id != data.get("id"):
        return
    trial_end = _utc_from_ts(data.get("trial_end"))
    if trial_end is not None:
        subscription.trial_ends_at = trial_end
        await db.flush()


async def handle_charge_refunded(db: AsyncSession, event: dict) -> None:
    """Reflect a refund on the corresponding ``platform_invoices`` row.

    Stripe fires ``charge.refunded`` whenever a refund is created on a
    charge — full or partial. Where the charge was generated by an
    invoice (i.e. ``charge.invoice`` is set), look up our local invoice
    by ``stripe_invoice_id`` and flip its status to ``refunded``
    (full) or ``partially_refunded`` (partial). Otherwise no-op — we
    don't track raw charges.

    Codex-12: a refund that arrives BEFORE its ``invoice.paid`` used to
    be dropped (dedup row committed, refund lost). It now leaves a
    placeholder invoice row carrying the refund status;
    ``record_paid_invoice`` fills in the details later and never
    overwrites the refund. Status only moves forward
    (partially_refunded → refunded), so a stale partial-refund event
    delivered after the full refund cannot downgrade it.
    """
    from app.platform.billing.models import Invoice
    from app.platform.billing.service import REFUND_STATUSES

    data = event.get("data", {}).get("object", {})
    invoice_id = data.get("invoice")
    if isinstance(invoice_id, dict):
        invoice_id = invoice_id.get("id")
    if not invoice_id:
        log.info("stripe.webhook.charge_refunded.no_invoice", charge_id=data.get("id"))
        return

    amount_refunded = int(data.get("amount_refunded", 0))
    invoice = (
        await db.execute(select(Invoice).where(Invoice.stripe_invoice_id == str(invoice_id)))
    ).scalar_one_or_none()
    if invoice is None:
        tenant_id = await _resolve_tenant_id(db, data)
        if tenant_id is None:
            # Not one of our customers (or not linked yet) — let Stripe
            # retry rather than lose the refund.
            log.warning(
                "stripe.webhook.charge_refunded.invoice_missing",
                charge_id=data.get("id"),
                stripe_invoice_id=invoice_id,
            )
            raise WebhookNotYetReady("refund for an unknown invoice and tenant")
        amount_charged = int(data.get("amount", 0))
        invoice = Invoice(
            tenant_id=tenant_id,
            stripe_invoice_id=str(invoice_id),
            amount_cents=amount_charged,
            currency=str(data.get("currency", "czk")).upper()[:3],
            status="refunded" if amount_refunded >= amount_charged > 0 else "partially_refunded",
        )
        db.add(invoice)
        await db.flush()
        log.info(
            "stripe.webhook.charge_refunded.placeholder",
            tenant_id=str(tenant_id),
            stripe_invoice_id=invoice_id,
            new_status=invoice.status,
        )
        return

    amount_charged = int(data.get("amount", 0)) or invoice.amount_cents
    fully_refunded = amount_refunded >= amount_charged > 0
    new_status = "refunded" if fully_refunded else "partially_refunded"
    if invoice.status == new_status:
        return  # already reflected
    if invoice.status == "refunded" and new_status in REFUND_STATUSES:
        return  # never step back from a full refund
    invoice.status = new_status
    await db.flush()
    log.info(
        "stripe.webhook.charge_refunded.applied",
        tenant_id=str(invoice.tenant_id),
        invoice_id=str(invoice.id),
        new_status=new_status,
        amount_refunded_cents=amount_refunded,
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
    "charge.refunded": handle_charge_refunded,
}


async def dispatch_webhook(db: AsyncSession, event: dict) -> None:
    """Route to the right handler based on ``event.type``. Unknown = no-op."""
    event_type = event.get("type", "")
    handler = HANDLERS.get(event_type)
    if handler is None:
        log.info("stripe.webhook.ignored", event_type=event_type)
        return
    await handler(db, event)
