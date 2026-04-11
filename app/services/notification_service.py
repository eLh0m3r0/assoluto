"""Notification service — figures out who gets which email.

All the DB lookups and recipient resolution live here; the HTTP handlers
only collect the BackgroundTasks-compatible arguments and schedule the
email task. This keeps the send-site tiny.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

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

    Returns None if there are no active admins to notify (silently
    skipped — still logged in the caller).
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
    """Prepare a status-change email.

    Recipients depend on who needs to know about the new state:
    - QUOTED / IN_PRODUCTION / READY / DELIVERED / CONFIRMED -> the
      customer's active contacts get notified by tenant staff actions
    - CONFIRMED / CANCELLED triggered by a contact -> tenant admins get
      notified

    Caller decides which kind to build based on actor type; this helper
    assumes tenant -> customer direction when called with a staff-driven
    transition and vice versa.
    """
    # We return ALL active contacts of the target customer. The caller
    # filters further when needed (e.g. internal-only transitions that
    # shouldn't leak to the customer side).
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


async def staff_recipients(db: AsyncSession) -> list[str]:
    """Return the e-mail addresses of active tenant admins (for customer->staff pings)."""
    return list(
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


# Helpers to discover UUIDs from notification builders — kept so routers
# can optionally rebuild payloads outside a live DB session.
_ = UUID
