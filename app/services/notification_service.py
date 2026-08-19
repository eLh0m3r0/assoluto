"""Notification service — figures out who gets which email.

All the DB lookups and recipient resolution live here; the HTTP
handlers only collect the ``BackgroundTasks``-compatible payloads and
schedule :func:`app.tasks.email_tasks.send_order_notification`.

### One payload, one email

:class:`OrderNotification` is frozen and carries exactly **one**
recipient. Every builder returns a ``list`` of them. That keeps retry
granularity, logging and digest merging per-recipient, and means the
whole subsystem needs a single task function rather than one per event.

### Audience resolution: one hard filter, two soft ones

Every event resolves through :func:`_select`, over the active rows on
the relevant side of the portal:

1. **consent** — ``notification_prefs`` says yes to this event. A *hard*
   filter. Nothing below may re-add a recipient dropped here.
2. **relevance** — the order is "theirs" under their
   :class:`~app.services.notification_prefs.NotificationScope`.
3. **reachability** — they have accepted their invitation, so the link
   in the mail opens something they can log in to.

Both soft filters **yield when they would empty the audience**. That is
the invariant that makes the rest safe: scope can be tightened
aggressively and pending invitations can be deprioritised, without ever
producing "the customer was never told about their own order" — from a
narrow scope on the only contact, or from that contact not having
accepted yet. Silence is the worse failure; a link to a login page at
least says something happened.

Consent must never be made to yield. Re-adding somebody who opted out is
a bug, not a safety net.

The actor is removed *first*, before any filter runs — see :func:`_select`
for why the ordering is load-bearing. Nobody needs an email about
something they just did themselves.

### Per-recipient locale

Each :class:`Recipient` carries its own locale, resolved via
:func:`app.services.locale_service.resolve_email_locale` from the
recipient row, the customer row (for contacts) and the tenant. Two
recipients of the same event can read it in different languages.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.logging import get_logger
from app.models.attachment import OrderAttachment
from app.models.customer import Customer, CustomerContact
from app.models.enums import STATUS_LABELS, OrderStatus
from app.models.order import Order, OrderComment, OrderStatusHistory
from app.models.tenant import Tenant
from app.models.user import User
from app.services.locale_service import resolve_email_locale
from app.services.notification_prefs import (
    NotificationEvent,
    NotificationPrefs,
    prefs_for_contact,
    prefs_for_user,
)

log = get_logger("app.notifications")

# structlog reserves the ``event`` key for the log message itself, so
# every notification log line namespaces ours as ``notification_event``.

#: Staff-side events important enough that a tenant with no staff at all
#: should still hear about them, via ``tenants.billing_email``.
_FALLBACK_EVENTS: frozenset[NotificationEvent] = frozenset(
    {
        NotificationEvent.ORDER_SUBMITTED,
        NotificationEvent.ORDER_STATUS_CHANGED,
        NotificationEvent.ORDER_COMMENT,
        NotificationEvent.ORDER_ATTACHMENT,
    }
)

#: Comment bodies are quoted in the email; keep the excerpt short enough
#: that the mail stays scannable and long enough to be useful without
#: opening the portal.
_EXCERPT_CHARS = 300


@dataclass(frozen=True)
class Recipient:
    email: str
    locale: str | None = None
    full_name: str = ""


@dataclass(frozen=True)
class OrderNotification:
    """One rendered-and-sent email, addressed to one person."""

    event: NotificationEvent
    recipient: Recipient
    tenant_name: str
    order_number: str
    order_title: str
    order_url: str
    #: Event-specific template context (author_name, status_label, …).
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def template(self) -> str:
        """Email template basename — the event value, by construction."""
        return self.event.value

    def context(self) -> dict[str, Any]:
        """Full render context for :func:`app.email.sender.render_email`."""
        return {
            "tenant_name": self.tenant_name,
            "order_number": self.order_number,
            "order_title": self.order_title,
            "order_url": self.order_url,
            "recipient_name": self.recipient.full_name,
            **self.extra,
        }


# --------------------------------------------------------------- helpers


def order_url(base_url: str, order: Order) -> str:
    return f"{base_url.rstrip('/')}/app/orders/{order.id}"


def _excerpt(body: str) -> str:
    text = (body or "").strip()
    if len(text) > _EXCERPT_CHARS:
        return text[: _EXCERPT_CHARS - 1] + "…"
    return text


@dataclass(frozen=True)
class _Candidate:
    prefs: NotificationPrefs
    recipient: Recipient
    #: Order is "theirs" under their scope — assigned to them (staff) or
    #: created/commented/uploaded by them (contact).
    involved: bool
    #: Has accepted their invitation, so the link in the mail actually
    #: opens something.
    accepted: bool


def _select(
    candidates: Sequence[_Candidate],
    *,
    event: NotificationEvent,
    exclude_email: str | None = None,
) -> list[Recipient]:
    """Apply the consent filter, then the two soft filters.

    One hard filter and two soft ones, in decreasing strength:

    * **consent** — ``prefs.wants(event)``. Absolute; a recipient dropped
      here is never re-added.
    * **relevance** — the order matches their scope.
    * **reachability** — they have accepted their invitation, so the link
      in the mail leads somewhere they can log in to.

    Each soft filter *yields* when it would empty the audience. That is
    what stops both "the customer heard nothing about their own order"
    (a narrow scope on the only contact) and "the customer heard nothing
    because their only contact has not accepted yet" — silence is the
    worse failure in both cases, and a link to a login page at least
    tells them something happened.

    The actor is dropped **first**, before any filter runs. Removing them
    afterwards would let them win a tier and take it with them: an
    accepted contact commenting on an order whose only other contact is
    still pending would satisfy the reachability filter alone, and
    stripping them at the end would leave nobody — silencing the very
    person who should have been told.
    """
    blocked = (exclude_email or "").strip().lower()
    pool = [c for c in candidates if c.recipient.email.strip().lower() != blocked]

    consented = [c for c in pool if c.prefs.wants(event)]
    if not consented:
        return []

    relevant = [c for c in consented if c.prefs.scope_covers(event, involved=c.involved)]
    tier = relevant or consented

    reachable = [c for c in tier if c.accepted]
    return _dedupe(c.recipient for c in (reachable or tier))


def _dedupe(recipients: Iterable[Recipient]) -> list[Recipient]:
    """Drop duplicate addresses, preserving order.

    Compared case-folded: a tenant can legitimately hold the same person
    as both a staff user and a customer contact, and ``User.email`` is
    lower-cased on write while a hand-typed address may not be.
    """
    seen: set[str] = set()
    out: list[Recipient] = []
    for recipient in recipients:
        key = recipient.email.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(recipient)
    return out


# ---------------------------------------------------- audience: staff side


async def _eligible_staff(db: AsyncSession) -> list[User]:
    """Every active staff user, both roles.

    Restricting this to ``TENANT_ADMIN`` was the original bug:
    ``tenant_staff`` is the default role and the pre-selected option in
    the invite form, so the people actually running production were the
    ones never told an order had arrived.

    Invited-but-not-accepted rows (``password_hash IS NULL``) stay in the
    list and are demoted by the reachability filter in :func:`_select`
    instead of dropped here — a tenant whose whole team is still pending
    should hear about an order, not lose it.
    """
    return list((await db.execute(select(User).where(User.is_active.is_(True)))).scalars().all())


async def resolve_staff_audience(
    db: AsyncSession,
    *,
    tenant: Tenant,
    settings: Settings,
    event: NotificationEvent,
    order: Order | None = None,
    exclude_email: str | None = None,
) -> list[Recipient]:
    """Who on the supplier side hears about ``event``."""
    rows = await _eligible_staff(db)

    if not rows:
        # No staff at all — not "everyone opted out", but "nobody is set
        # up to hear this". Route to the tenant's billing address so an
        # order submission can never vanish silently.
        if event in _FALLBACK_EVENTS and tenant.billing_email:
            log.warning(
                "notifications.staff_fallback",
                notification_event=event.value,
                tenant_id=str(tenant.id),
                reason="no_eligible_staff",
            )
            if tenant.billing_email.strip().lower() == (exclude_email or "").strip().lower():
                return []
            return [
                Recipient(
                    email=tenant.billing_email,
                    locale=resolve_email_locale(tenant=tenant, settings=settings),
                )
            ]
        return []

    assignee_id = getattr(order, "assigned_to_user_id", None) if order is not None else None
    candidates = [
        _Candidate(
            prefs=prefs_for_user(user),
            recipient=Recipient(
                email=user.email,
                locale=resolve_email_locale(
                    recipient=user, customer=None, tenant=tenant, settings=settings
                ),
                full_name=user.full_name,
            ),
            # An unassigned order belongs to nobody, so it belongs to
            # everyone: "only orders assigned to me" must not quietly bin
            # work that has not been triaged yet. Without this the
            # preference page's promise — "an order with nobody assigned
            # still reaches everyone" — held only when *every* staff
            # member had narrowed their scope, which is the one case
            # where it does not matter.
            involved=assignee_id is None or user.id == assignee_id,
            accepted=user.password_hash is not None,
        )
        for user in rows
    ]
    recipients = _select(candidates, event=event, exclude_email=exclude_email)
    if not recipients:
        log.info(
            "notifications.no_recipients",
            notification_event=event.value,
            side="staff",
            tenant_id=str(tenant.id),
        )
    return recipients


# -------------------------------------------------- audience: customer side


async def _involved_contact_ids(db: AsyncSession, order: Order) -> set[UUID]:
    """Contacts who have touched this order: created, commented, uploaded."""
    involved: set[UUID] = set()
    if order.created_by_contact_id is not None:
        involved.add(order.created_by_contact_id)

    comment_rows = (
        await db.execute(
            select(OrderComment.author_contact_id).where(
                OrderComment.order_id == order.id,
                OrderComment.author_contact_id.is_not(None),
            )
        )
    ).all()
    involved.update(row[0] for row in comment_rows)

    attachment_rows = (
        await db.execute(
            select(OrderAttachment.uploaded_by_contact_id).where(
                OrderAttachment.order_id == order.id,
                OrderAttachment.uploaded_by_contact_id.is_not(None),
            )
        )
    ).all()
    involved.update(row[0] for row in attachment_rows)

    # Driving a transition counts too. Contacts may only move QUOTED ->
    # CONFIRMED and -> CANCELLED, so this is the person who accepted or
    # killed the quote — the most committing act available to them. Left
    # out, somebody who did nothing else would stay "uninvolved" and hear
    # nothing more about the order they just signed off.
    transition_rows = (
        await db.execute(
            select(OrderStatusHistory.changed_by_contact_id).where(
                OrderStatusHistory.order_id == order.id,
                OrderStatusHistory.changed_by_contact_id.is_not(None),
            )
        )
    ).all()
    involved.update(row[0] for row in transition_rows)
    return involved


async def resolve_contact_audience(
    db: AsyncSession,
    *,
    tenant: Tenant,
    settings: Settings,
    event: NotificationEvent,
    order: Order,
    customer: Customer | None = None,
    exclude_email: str | None = None,
) -> list[Recipient]:
    """Who on the customer side hears about ``event``."""
    rows = list(
        (
            await db.execute(
                # Pending invitations stay in: the reachability filter in
                # ``_select`` demotes them, so an accepted colleague wins,
                # but a customer whose only contact has not accepted yet
                # still hears about their order.
                select(CustomerContact).where(
                    CustomerContact.customer_id == order.customer_id,
                    CustomerContact.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []

    if customer is None:
        customer = (
            await db.execute(select(Customer).where(Customer.id == order.customer_id))
        ).scalar_one_or_none()

    # Only pay for the involvement queries when somebody actually narrows
    # their scope — for an all-``ALL`` customer they cannot change the result.
    prefs_by_contact = {contact.id: prefs_for_contact(contact) for contact in rows}
    involved_ids: set[UUID] = set()
    if any(not prefs.scope_covers(event, involved=False) for prefs in prefs_by_contact.values()):
        involved_ids = await _involved_contact_ids(db, order)

    candidates = [
        _Candidate(
            prefs=prefs_by_contact[contact.id],
            recipient=Recipient(
                email=contact.email,
                locale=resolve_email_locale(
                    recipient=contact, customer=customer, tenant=tenant, settings=settings
                ),
                full_name=contact.full_name,
            ),
            involved=contact.id in involved_ids,
            accepted=contact.accepted_at is not None,
        )
        for contact in rows
    ]
    recipients = _select(candidates, event=event, exclude_email=exclude_email)
    if not recipients:
        log.info(
            "notifications.no_recipients",
            notification_event=event.value,
            side="contact",
            tenant_id=str(tenant.id),
        )
    return recipients


# -------------------------------------------------------------- builders


def _fan_out(
    recipients: Sequence[Recipient],
    *,
    event: NotificationEvent,
    tenant: Tenant,
    order: Order,
    base_url: str,
    extra: dict[str, Any] | None = None,
) -> list[OrderNotification]:
    return [
        OrderNotification(
            event=event,
            recipient=recipient,
            tenant_name=tenant.name,
            order_number=order.number,
            order_title=order.title,
            order_url=order_url(base_url, order),
            extra=dict(extra or {}),
        )
        for recipient in recipients
    ]


async def build_order_submitted(
    db: AsyncSession,
    *,
    tenant: Tenant,
    order: Order,
    base_url: str,
    settings: Settings,
    actor_email: str | None = None,
) -> list[OrderNotification]:
    """A customer (or staff on their behalf) submitted an order.

    ``actor_email`` is excluded — staff may drive DRAFT → SUBMITTED
    themselves (``STAFF_ALLOWED_TRANSITIONS`` is unrestricted, CLAUDE.md
    §18), and emailing somebody about their own click is noise.
    """
    event = NotificationEvent.ORDER_SUBMITTED
    recipients = await resolve_staff_audience(
        db,
        tenant=tenant,
        settings=settings,
        event=event,
        order=order,
        exclude_email=actor_email,
    )
    if not recipients:
        return []
    customer = (
        await db.execute(select(Customer).where(Customer.id == order.customer_id))
    ).scalar_one_or_none()
    return _fan_out(
        recipients,
        event=event,
        tenant=tenant,
        order=order,
        base_url=base_url,
        extra={"customer_name": customer.name if customer else ""},
    )


async def build_order_created(
    db: AsyncSession,
    *,
    tenant: Tenant,
    order: Order,
    base_url: str,
    settings: Settings,
    author_name: str = "",
    actor_email: str | None = None,
) -> list[OrderNotification]:
    """Staff opened an order on a customer's behalf — tell the customer.

    Without this the customer heard nothing until the first status
    transition, which for a phone-agreed job could be days later.
    """
    event = NotificationEvent.ORDER_CREATED
    recipients = await resolve_contact_audience(
        db,
        tenant=tenant,
        settings=settings,
        event=event,
        order=order,
        exclude_email=actor_email,
    )
    return _fan_out(
        recipients,
        event=event,
        tenant=tenant,
        order=order,
        base_url=base_url,
        extra={"author_name": author_name},
    )


async def build_order_status_changed(
    db: AsyncSession,
    *,
    tenant: Tenant,
    order: Order,
    to_status: OrderStatus,
    base_url: str,
    settings: Settings,
    actor_is_contact: bool = False,
    actor_email: str | None = None,
) -> list[OrderNotification]:
    """Tell *the other side* that the order moved.

    Staff moved it -> the customer's contacts. A contact moved it -> the
    supplier's staff: contacts may only do QUOTED -> CONFIRMED and
    -> CANCELLED, so that is the customer accepting or killing a quote,
    the most commercially loaded event in the product.
    """
    event = NotificationEvent.ORDER_STATUS_CHANGED
    if actor_is_contact:
        recipients = await resolve_staff_audience(
            db,
            tenant=tenant,
            settings=settings,
            event=event,
            order=order,
            exclude_email=actor_email,
        )
    else:
        recipients = await resolve_contact_audience(
            db,
            tenant=tenant,
            settings=settings,
            event=event,
            order=order,
            exclude_email=actor_email,
        )
    return _fan_out(
        recipients,
        event=event,
        tenant=tenant,
        order=order,
        base_url=base_url,
        extra={
            "status_label": STATUS_LABELS.get(to_status, to_status.value),
            "status_value": to_status.value,
        },
    )


async def build_order_comment(
    db: AsyncSession,
    *,
    tenant: Tenant,
    order: Order,
    author_email: str,
    author_name: str,
    author_is_staff: bool,
    body: str,
    base_url: str,
    settings: Settings,
) -> list[OrderNotification]:
    """Notify the other side of a non-internal comment.

    Internal comments must never reach a contact; the caller is
    responsible for not invoking this for them.
    """
    event = NotificationEvent.ORDER_COMMENT
    if author_is_staff:
        recipients = await resolve_contact_audience(
            db,
            tenant=tenant,
            settings=settings,
            event=event,
            order=order,
            exclude_email=author_email,
        )
    else:
        recipients = await resolve_staff_audience(
            db,
            tenant=tenant,
            settings=settings,
            event=event,
            order=order,
            exclude_email=author_email,
        )
    return _fan_out(
        recipients,
        event=event,
        tenant=tenant,
        order=order,
        base_url=base_url,
        extra={"author_name": author_name, "body_excerpt": _excerpt(body)},
    )


async def build_order_attachment(
    db: AsyncSession,
    *,
    tenant: Tenant,
    order: Order,
    uploader_email: str,
    uploader_name: str,
    uploader_is_staff: bool,
    filename: str,
    base_url: str,
    settings: Settings,
) -> list[OrderNotification]:
    """Notify the other side that a file landed on the order.

    In a job shop this is the highest-value notification there is — a
    revised drawing that nobody sees is scrap metal — and it previously
    sent nothing at all.
    """
    event = NotificationEvent.ORDER_ATTACHMENT
    if uploader_is_staff:
        recipients = await resolve_contact_audience(
            db,
            tenant=tenant,
            settings=settings,
            event=event,
            order=order,
            exclude_email=uploader_email,
        )
    else:
        recipients = await resolve_staff_audience(
            db,
            tenant=tenant,
            settings=settings,
            event=event,
            order=order,
            exclude_email=uploader_email,
        )
    return _fan_out(
        recipients,
        event=event,
        tenant=tenant,
        order=order,
        base_url=base_url,
        extra={"author_name": uploader_name, "filename": filename},
    )


def build_order_assigned(
    *,
    tenant: Tenant,
    order: Order,
    assignee: User,
    base_url: str,
    settings: Settings,
    actor_email: str | None = None,
    actor_name: str = "",
) -> list[OrderNotification]:
    """Tell one staff member the order is now theirs.

    Not an audience — a single addressee — so this skips
    :func:`resolve_staff_audience` and only checks the assignee's own
    consent. Assigning to yourself sends nothing.
    """
    event = NotificationEvent.ORDER_ASSIGNED
    # Reachability is a *soft* filter everywhere else because there is an
    # audience to fall back on. Here there is exactly one addressee, so it
    # has nothing to yield to and stays hard: mailing "this is yours" to
    # somebody who cannot log in helps nobody.
    if not assignee.is_active or assignee.password_hash is None:
        return []
    if not prefs_for_user(assignee).wants(event):
        return []
    recipient = Recipient(
        email=assignee.email,
        locale=resolve_email_locale(
            recipient=assignee, customer=None, tenant=tenant, settings=settings
        ),
        full_name=assignee.full_name,
    )
    if recipient.email.strip().lower() == (actor_email or "").strip().lower():
        return []
    return _fan_out(
        [recipient],
        event=event,
        tenant=tenant,
        order=order,
        base_url=base_url,
        extra={
            "author_name": actor_name,
            "status_label": STATUS_LABELS.get(order.status, order.status.value),
        },
    )


# ---------------------------------------------------------------- digest


#: Template used when several orders of the same event collapse into one
#: mail. Not a :class:`NotificationEvent` member — it is a rendering
#: choice, not a thing that happened, and nobody subscribes to it
#: separately.
DIGEST_TEMPLATE = "order_digest"


@dataclass(frozen=True)
class OrderDigestNotification:
    """Several same-event notifications for one recipient, as one email."""

    event: NotificationEvent
    recipient: Recipient
    tenant_name: str
    orders: list[dict[str, Any]]

    @property
    def template(self) -> str:
        return DIGEST_TEMPLATE

    def context(self) -> dict[str, Any]:
        return {
            "tenant_name": self.tenant_name,
            "recipient_name": self.recipient.full_name,
            "orders": self.orders,
            "order_count": len(self.orders),
        }


AnyNotification = OrderNotification | OrderDigestNotification


def merge_for_digest(
    notifications: Iterable[OrderNotification],
) -> list[AnyNotification]:
    """Collapse per-order payloads into one email per recipient per event.

    A bulk transition over 30 orders used to schedule 30 separate sends
    to every recipient. Grouping is by ``(recipient email, event)``;
    a recipient with a single order keeps the normal single-order
    template, so nothing changes for the non-bulk path.

    Pure function over the payload list — no queue, no periodic job, no
    persisted state.
    """
    grouped: dict[tuple[str, NotificationEvent], list[OrderNotification]] = {}
    order_of_keys: list[tuple[str, NotificationEvent]] = []
    for notification in notifications:
        key = (notification.recipient.email.strip().lower(), notification.event)
        if key not in grouped:
            grouped[key] = []
            order_of_keys.append(key)
        grouped[key].append(notification)

    out: list[AnyNotification] = []
    for key in order_of_keys:
        batch = grouped[key]
        if len(batch) == 1:
            out.append(batch[0])
            continue
        first = batch[0]
        out.append(
            OrderDigestNotification(
                event=first.event,
                recipient=first.recipient,
                tenant_name=first.tenant_name,
                orders=[
                    {
                        "number": item.order_number,
                        "title": item.order_title,
                        "url": item.order_url,
                        "status_label": item.extra.get("status_label", ""),
                    }
                    for item in batch
                ],
            )
        )
    return out
