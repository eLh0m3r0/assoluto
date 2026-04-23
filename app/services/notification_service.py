"""Notification service — figures out who gets which email.

All the DB lookups and recipient resolution live here; the HTTP
handlers only collect the ``BackgroundTasks``-compatible arguments
and schedule the email task. This keeps the send-site tiny.

### Per-recipient locale

Each notification payload carries a ``recipients_with_locale`` list —
``(email, locale_or_none)`` tuples — rather than a flat list of
addresses. The locale is resolved per-recipient via
:func:`app.services.locale_service.resolve_email_locale` at build
time using the recipient row, the customer row (for contacts), and
the tenant row. The email task renders once per tuple so two
recipients on the same notification list can each receive the
message in their own language.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.customer import Customer, CustomerContact
from app.models.enums import OrderStatus, UserRole
from app.models.order import Order
from app.models.tenant import Tenant
from app.models.user import User
from app.services.locale_service import resolve_email_locale


@dataclass(frozen=True)
class OrderSubmittedNotification:
    tenant_name: str
    customer_name: str
    order_number: str
    order_title: str
    order_url: str
    recipients_with_locale: list[tuple[str, str | None]]


@dataclass(frozen=True)
class OrderStatusChangedNotification:
    tenant_name: str
    order_number: str
    order_title: str
    order_url: str
    to_status: OrderStatus
    recipients_with_locale: list[tuple[str, str | None]]


@dataclass(frozen=True)
class OrderCommentNotification:
    tenant_name: str
    order_number: str
    order_title: str
    order_url: str
    author_name: str
    body_excerpt: str
    recipients_with_locale: list[tuple[str, str | None]]


async def _staff_recipients(
    db: AsyncSession,
    *,
    tenant: Tenant,
    settings: Settings,
    exclude_email: str | None = None,
) -> list[tuple[str, str | None]]:
    """Return (email, locale) tuples for every active tenant admin."""
    stmt = select(User).where(User.role == UserRole.TENANT_ADMIN, User.is_active.is_(True))
    if exclude_email:
        stmt = stmt.where(User.email != exclude_email)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        (
            u.email,
            resolve_email_locale(recipient=u, customer=None, tenant=tenant, settings=settings),
        )
        for u in rows
    ]


async def _contact_recipients(
    db: AsyncSession,
    *,
    customer_id,
    tenant: Tenant,
    settings: Settings,
    exclude_email: str | None = None,
) -> list[tuple[str, str | None]]:
    """Return (email, locale) tuples for every active contact of ``customer_id``."""
    stmt = select(CustomerContact).where(
        CustomerContact.customer_id == customer_id,
        CustomerContact.is_active.is_(True),
    )
    if exclude_email:
        stmt = stmt.where(CustomerContact.email != exclude_email)
    rows = (await db.execute(stmt)).scalars().all()
    if not rows:
        return []
    customer = (
        await db.execute(select(Customer).where(Customer.id == customer_id))
    ).scalar_one_or_none()
    return [
        (
            c.email,
            resolve_email_locale(recipient=c, customer=customer, tenant=tenant, settings=settings),
        )
        for c in rows
    ]


async def build_order_submitted(
    db: AsyncSession,
    *,
    tenant: Tenant,
    order: Order,
    base_url: str,
    settings: Settings,
) -> OrderSubmittedNotification | None:
    """Prepare the payload for a new-order email to tenant admins.

    Returns None if there are no active admins to notify.
    """
    recipients = await _staff_recipients(db, tenant=tenant, settings=settings)
    if not recipients:
        return None

    customer = (
        await db.execute(select(Customer).where(Customer.id == order.customer_id))
    ).scalar_one_or_none()

    return OrderSubmittedNotification(
        tenant_name=tenant.name,
        customer_name=customer.name if customer else "",
        order_number=order.number,
        order_title=order.title,
        order_url=f"{base_url.rstrip('/')}/app/orders/{order.id}",
        recipients_with_locale=recipients,
    )


async def build_order_status_changed(
    db: AsyncSession,
    *,
    tenant: Tenant,
    order: Order,
    to_status: OrderStatus,
    base_url: str,
    settings: Settings,
) -> OrderStatusChangedNotification | None:
    """Prepare a status-change email for the order's customer contacts.

    Returns all active contacts of the target customer. The caller is
    expected to avoid building this payload for transitions that
    shouldn't leak to the customer side.
    """
    recipients = await _contact_recipients(
        db, customer_id=order.customer_id, tenant=tenant, settings=settings
    )
    if not recipients:
        return None

    return OrderStatusChangedNotification(
        tenant_name=tenant.name,
        order_number=order.number,
        order_title=order.title,
        order_url=f"{base_url.rstrip('/')}/app/orders/{order.id}",
        to_status=to_status,
        recipients_with_locale=recipients,
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
) -> OrderCommentNotification | None:
    """Prepare a comment notification.

    - Staff comment (non-internal) -> all active contacts of the customer.
    - Contact comment -> all active tenant admins.
    Internal comments are never notified to contacts; this function
    assumes the caller only invokes it for non-internal comments.
    """
    if author_is_staff:
        recipients = await _contact_recipients(
            db,
            customer_id=order.customer_id,
            tenant=tenant,
            settings=settings,
            exclude_email=author_email,
        )
    else:
        recipients = await _staff_recipients(
            db, tenant=tenant, settings=settings, exclude_email=author_email
        )

    if not recipients:
        return None

    excerpt = body.strip()
    if len(excerpt) > 300:
        excerpt = excerpt[:297] + "…"

    return OrderCommentNotification(
        tenant_name=tenant.name,
        order_number=order.number,
        order_title=order.title,
        order_url=f"{base_url.rstrip('/')}/app/orders/{order.id}",
        author_name=author_name,
        body_excerpt=excerpt,
        recipients_with_locale=recipients,
    )
