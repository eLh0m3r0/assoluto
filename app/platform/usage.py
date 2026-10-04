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
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging import get_logger
from app.models.attachment import OrderAttachment
from app.models.customer import CustomerContact
from app.models.order import Order
from app.models.user import User
from app.platform.billing.models import Plan
from app.platform.billing.service import get_subscription_for_tenant
from app.platform.models import MEMBERSHIP_ACCESS_SUPPORT, TenantMembership

log = get_logger("app.billing")


def _support_user_ids():
    """User ids that are a platform operator's support-access grant.

    LOGIC-20: such a user is a TENANT_ADMIN row in the customer's tenant
    but is not the customer's staff — it must not eat a paid seat (a
    Starter tenant with 3 staff showed 4/3 while support was granted)
    nor receive the tenant's mail.
    """
    return select(TenantMembership.user_id).where(
        TenantMembership.access_type == MEMBERSHIP_ACCESS_SUPPORT,
        TenantMembership.user_id.is_not(None),
    )


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
    users_q = select(func.count(User.id)).where(
        User.tenant_id == tenant_id,
        User.is_active,
        User.id.not_in(_support_user_ids()),
    )
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


# Advisory-lock namespace (two-int form) for "count usage, then insert".
# See ``ensure_within_limit``.
PLAN_LIMIT_LOCK_NAMESPACE = 42_102


async def ensure_within_limit(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    metric: str,
    delta: int = 1,
    soft: bool = False,
) -> None:
    """Raise :class:`PlanLimitExceeded` if ``current + delta > plan_limit``.

    ``metric`` is one of ``users`` / ``contacts`` / ``orders`` / ``storage_mb``.
    If the tenant has no active subscription (self-hosted fallback) no
    limit is applied. Plan ``community`` also has no caps.

    **Serialised per tenant (Codex-10).** Counting and inserting are two
    statements; two concurrent requests used to both see the last free
    slot and both succeed. When a cap applies we take a transaction-
    scoped advisory lock on the tenant before counting, held until the
    caller's transaction commits — i.e. through the INSERT that follows.

    **``soft=True`` — the action of a supplier's CUSTOMER (LOGIC-3).**
    A customer contact uploading a drawing or creating an order must
    never hit the supplier's "upgrade your plan" page: they cannot act
    on it, and a bounced drawing that nobody at the supplier hears about
    is the worst outcome. Over the cap the action is accepted, logged,
    and the tenant's administrators get an e-mail (at most one per
    metric per day).
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

    await db.execute(
        text("SELECT pg_advisory_xact_lock(:ns, hashtext(:tid))"),
        {"ns": PLAN_LIMIT_LOCK_NAMESPACE, "tid": str(tenant_id)},
    )

    usage = await snapshot_tenant_usage(db, tenant_id)
    current = {
        "users": usage.users,
        "contacts": usage.contacts,
        "orders": usage.orders_this_month,
        "storage_mb": usage.storage_bytes // (1024 * 1024),
    }[metric]

    if current + delta > limit:
        if soft:
            log.warning(
                "plan_limit.exceeded_soft",
                tenant_id=str(tenant_id),
                metric=metric,
                limit=limit,
                current=current,
            )
            await _alert_tenant_admins(db, tenant_id=tenant_id, metric=metric, limit=limit)
            return
        raise PlanLimitExceeded(metric=metric, limit=limit, current=current)


# ----------------------------------------------- over-limit admin alert

# (tenant_id, metric) → last alert. In-process throttle: production runs
# one worker, so this bounds the mail to ~1 per metric per day (plus at
# most one extra after a restart) without a table for it.
_ALERT_INTERVAL = timedelta(hours=24)
_last_alert: dict[tuple[UUID, str], datetime] = {}


def _alert_sender(settings):  # pragma: no cover - patched in tests
    from app.email.sender import build_sender

    return build_sender(settings)


def _dispatch(fn) -> None:  # pragma: no cover - patched in tests
    """Run the blocking SMTP send off the event loop, fire-and-forget.

    The mail needs nothing from the DB (recipients are resolved before),
    so it does not have to wait for the request's commit (CLAUDE.md §2).
    """
    import asyncio

    asyncio.get_running_loop().run_in_executor(None, fn)


async def _alert_tenant_admins(
    db: AsyncSession, *, tenant_id: UUID, metric: str, limit: int
) -> None:
    from functools import partial

    from app.config import get_settings
    from app.models.enums import UserRole
    from app.models.tenant import Tenant
    from app.services.locale_service import resolve_email_locale
    from app.tasks.email_tasks import _render_and_send

    now = datetime.now(UTC)
    key = (tenant_id, metric)
    last = _last_alert.get(key)
    if last is not None and now - last < _ALERT_INTERVAL:
        return

    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if tenant is None:
        return
    admins = (
        (
            await db.execute(
                select(User).where(
                    User.tenant_id == tenant_id,
                    User.role == UserRole.TENANT_ADMIN,
                    User.is_active.is_(True),
                    User.password_hash.is_not(None),
                    User.id.not_in(_support_user_ids()),
                )
            )
        )
        .scalars()
        .all()
    )
    if not admins:
        return
    _last_alert[key] = now

    settings = get_settings()
    sender = _alert_sender(settings)
    billing_url = settings.app_base_url.rstrip("/") + f"/platform/billing?tenant={tenant.slug}"
    for admin in admins:
        locale = resolve_email_locale(recipient=admin, tenant=tenant, settings=settings)
        _dispatch(
            partial(
                _render_and_send,
                sender,
                "plan_limit_reached",
                "plan_limit_reached",
                admin.email,
                {
                    "full_name": admin.full_name,
                    "tenant_name": tenant.name,
                    "metric": metric,
                    "limit": limit,
                    "billing_url": billing_url,
                },
                locale,
            )
        )
