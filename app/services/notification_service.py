"""Notification service — figures out who gets which email.

All the DB lookups and recipient resolution live here; the HTTP handlers
only collect the BackgroundTasks-compatible arguments and schedule the
email task. This keeps the send-site tiny.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer, CustomerContact
from app.models.enums import OrderStatus, UserRole
from app.models.order import Order
from app.models.user import User


@dataclass(frozen=True)
class OrderSubmittedNotification:
    tenant_name: str
    customer_name: str
    order_number: str
    order_title: str
    order_url: str
    recipients: list[str]


@dataclass(frozen=True)
class OrderStatusChangedNotification:
    tenant_name: str
    order_number: str
    order_title: str
    order_url: str
    to_status: OrderStatus
    recipients: list[str]


async def build_order_submitted(
    db: AsyncSession,
    *,
    tenant_name: str,
    order: Order,
    base_url: str,
) -> OrderSubmittedNotification | None:
    """Prepare the payload for a new-order email to tenant admins.

    Returns None if there are no active admins to notify.
    """
    recipients = (
        (
            await db.execute(
                select(User.email).where(
                    User.role == UserRole.TENANT_ADMIN, User.is_active.is_(True)
                )
            )
        )
        .scalars()
        .all()
    )
    if not recipients:
        return None

    customer = (
        await db.execute(select(Customer).where(Customer.id == order.customer_id))
    ).scalar_one_or_none()

    return OrderSubmittedNotification(
        tenant_name=tenant_name,
        customer_name=customer.name if customer else "",
        order_number=order.number,
        order_title=order.title,
        order_url=f"{base_url.rstrip('/')}/app/orders/{order.id}",
        recipients=list(recipients),
    )


async def build_order_status_changed(
    db: AsyncSession,
    *,
    tenant_name: str,
    order: Order,
    to_status: OrderStatus,
    base_url: str,
) -> OrderStatusChangedNotification | None:
    """Prepare a status-change email for the order's customer contacts.

    Returns all active contacts of the target customer. The caller is
    expected to avoid building this payload for transitions that
    shouldn't leak to the customer side.
    """
    recipients = (
        (
            await db.execute(
                select(CustomerContact.email).where(
                    CustomerContact.customer_id == order.customer_id,
                    CustomerContact.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    if not recipients:
        return None

    return OrderStatusChangedNotification(
        tenant_name=tenant_name,
        order_number=order.number,
        order_title=order.title,
        order_url=f"{base_url.rstrip('/')}/app/orders/{order.id}",
        to_status=to_status,
        recipients=list(recipients),
    )


@dataclass(frozen=True)
class OrderCommentNotification:
    tenant_name: str
    order_number: str
    order_title: str
    order_url: str
    author_name: str
    body_excerpt: str
    recipients: list[str]


async def build_order_comment(
    db: AsyncSession,
    *,
    tenant_name: str,
    order: Order,
    author_email: str,
    author_name: str,
    author_is_staff: bool,
    body: str,
    base_url: str,
) -> OrderCommentNotification | None:
    """Prepare a comment notification.

    - Staff comment (non-internal) -> all active contacts of the customer.
    - Contact comment -> all active tenant admins.
    Internal comments are never notified to contacts; this function
    assumes the caller only invokes it for non-internal comments.
    """
    if author_is_staff:
        rows = (
            (
                await db.execute(
                    select(CustomerContact.email).where(
                        CustomerContact.customer_id == order.customer_id,
                        CustomerContact.is_active.is_(True),
                        CustomerContact.email != author_email,
                    )
                )
            )
            .scalars()
            .all()
        )
    else:
        rows = (
            (
                await db.execute(
                    select(User.email).where(
                        User.role == UserRole.TENANT_ADMIN,
                        User.is_active.is_(True),
                        User.email != author_email,
                    )
                )
            )
            .scalars()
            .all()
        )

    if not rows:
        return None

    excerpt = body.strip()
    if len(excerpt) > 300:
        excerpt = excerpt[:297] + "…"

    return OrderCommentNotification(
        tenant_name=tenant_name,
        order_number=order.number,
        order_title=order.title,
        order_url=f"{base_url.rstrip('/')}/app/orders/{order.id}",
        author_name=author_name,
        body_excerpt=excerpt,
        recipients=list(rows),
    )
