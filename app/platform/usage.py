"""Usage metering + plan limit enforcement.

Queries live data (orders, users, contacts, attachment byte totals) to
produce a :class:`UsageSnapshot` that the billing dashboard displays and
the ``check_plan_limits`` dependency enforces against the current plan.

All reads go through the **owner** session (bypassing RLS) because limit
enforcement needs to count rows across tenants (each caller supplies a
``tenant_id``; the queries filter explicitly). Enforcement is a soft
check — if the tenant has no subscription (self-hosted / older signup)
no limits apply.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attachment import OrderAttachment
from app.models.customer import CustomerContact
from app.models.order import Order
from app.models.user import User
from app.platform.billing.models import Plan
from app.platform.billing.service import get_subscription_for_tenant


@dataclass(frozen=True)
class UsageSnapshot:
    users: int
    contacts: int
    orders_this_month: int
    storage_bytes: int

    def percent_of(self, metric: str, limit: int | None) -> int:
        """Return integer percent (0..100+) of ``limit`` used for ``metric``.

        ``metric`` is one of: users, contacts, orders, storage_mb. Unknown
        metrics and ``None`` limits return 0 (treated as unlimited).
        """
        if limit is None or limit <= 0:
            return 0
        value = self._map.get(metric, 0)  # type: ignore[attr-defined]
        return max(0, min(200, int(value / limit * 100)))

    def __post_init__(self) -> None:
        # Pre-compute a lookup table so percent_of stays cheap.
        object.__setattr__(
            self,
            "_map",
            {
                "users": self.users,
                "contacts": self.contacts,
                "orders": self.orders_this_month,
                "storage_mb": self.storage_bytes // (1024 * 1024),
            },
        )


async def snapshot_tenant_usage(db: AsyncSession, tenant_id: UUID) -> UsageSnapshot:
    """Compute current usage for a tenant.

    Queries are all indexed on ``tenant_id``. Cheap enough to run on
    every request that needs it; if profiling says otherwise we can
    cache per-tenant for 60 s.
    """
    users_q = select(func.count(User.id)).where(User.tenant_id == tenant_id, User.is_active)
    users = int((await db.execute(users_q)).scalar_one())

    contacts_q = select(func.count(CustomerContact.id)).where(
        CustomerContact.tenant_id == tenant_id, CustomerContact.is_active
    )
    contacts = int((await db.execute(contacts_q)).scalar_one())

    # Orders this calendar month.
    now = datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    orders_q = select(func.count(Order.id)).where(
        Order.tenant_id == tenant_id, Order.created_at >= month_start
    )
    orders = int((await db.execute(orders_q)).scalar_one())

    storage_q = select(func.coalesce(func.sum(OrderAttachment.size_bytes), 0)).where(
        OrderAttachment.tenant_id == tenant_id
    )
    storage_bytes = int((await db.execute(storage_q)).scalar_one())

    return UsageSnapshot(
        users=users,
        contacts=contacts,
        orders_this_month=orders,
        storage_bytes=storage_bytes,
    )


# ----------------------------------------------------------- enforcement


class PlanLimitExceeded(Exception):
    """Raised when a creation would push a tenant over its plan cap."""

    def __init__(self, metric: str, limit: int, current: int) -> None:
        super().__init__(f"Plan limit exceeded for {metric}: current={current}, limit={limit}")
        self.metric = metric
        self.limit = limit
        self.current = current


async def ensure_within_limit(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    metric: str,
    delta: int = 1,
) -> None:
    """Raise :class:`PlanLimitExceeded` if ``current + delta > plan_limit``.

    ``metric`` is one of ``users`` / ``contacts`` / ``orders`` / ``storage_mb``.
    If the tenant has no active subscription (self-hosted fallback) no
    limit is applied. Plan ``community`` also has no caps.
    """
    sub = await get_subscription_for_tenant(db, tenant_id)
    if sub is None:
        return

    plan = (await db.execute(select(Plan).where(Plan.id == sub.plan_id))).scalar_one_or_none()
    if plan is None:
        return

    limit_col_map = {
        "users": plan.max_users,
        "contacts": plan.max_contacts,
        "orders": plan.max_orders_per_month,
        "storage_mb": plan.max_storage_mb,
    }
    limit = limit_col_map.get(metric)
    if limit is None or limit <= 0:
        return  # unlimited

    usage = await snapshot_tenant_usage(db, tenant_id)
    current = {
        "users": usage.users,
        "contacts": usage.contacts,
        "orders": usage.orders_this_month,
        "storage_mb": usage.storage_bytes // (1024 * 1024),
    }[metric]

    if current + delta > limit:
        raise PlanLimitExceeded(metric=metric, limit=limit, current=current)
