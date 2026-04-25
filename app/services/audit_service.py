"""Audit log service — record domain actions to ``audit_events``.

The service is deliberately thin: it resolves a consistent
:class:`ActorInfo` from whatever the caller already has (a
:class:`~app.deps.Principal`, a bare ``User``/``CustomerContact``, or
``None`` for system jobs), builds a JSON diff, and inserts a row in the
**same transaction** as the business mutation. No commit, no flush
outside of what SQLAlchemy does on its own — that way audit rows are
atomic with the change they describe: a roll-back of the outer
transaction also rolls back the audit entry.

Reads (:func:`list_events`) are primarily gated by Postgres RLS, which
is already active on the session. On top of that, customer contacts
can only see events on their own customer's orders; staff see
everything inside their tenant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Select, and_, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_event import AuditEvent
from app.models.order import Order

# ---------------------------------------------------------------------------
# Actor resolution
# ---------------------------------------------------------------------------


ActorType = Literal["user", "contact", "system"]


@dataclass(frozen=True)
class ActorInfo:
    """Who performed the action.

    ``id`` is ``None`` for the ``system`` actor (background jobs, CLI
    migrations). ``label`` is a denormalised display string so the row
    stays meaningful after the underlying principal is deactivated.
    """

    type: ActorType
    id: UUID | None
    label: str


SYSTEM_ACTOR = ActorInfo(type="system", id=None, label="system")


def actor_from_principal(principal: Any) -> ActorInfo:
    """Map a :class:`app.deps.Principal` (or ``None``) to an ``ActorInfo``.

    Accepts any duck-typed object with ``type``, ``id``, ``full_name``
    and ``email`` attributes so unit tests can pass lightweight stand-ins
    without importing the FastAPI-flavoured ``Principal``.
    """
    if principal is None:
        return SYSTEM_ACTOR

    actor_type = getattr(principal, "type", None)
    if actor_type not in ("user", "contact"):
        return SYSTEM_ACTOR

    principal_id = getattr(principal, "id", None)
    if not isinstance(principal_id, UUID):
        # Guard against stray strings — the type narrowing matters for
        # the column.
        try:
            principal_id = UUID(str(principal_id)) if principal_id is not None else None
        except (TypeError, ValueError):
            principal_id = None

    label = (
        getattr(principal, "full_name", None)
        or getattr(principal, "email", None)
        or str(principal_id or "unknown")
    )
    return ActorInfo(type=actor_type, id=principal_id, label=str(label)[:255])


# ---------------------------------------------------------------------------
# Diff helpers
# ---------------------------------------------------------------------------


def _json_safe(value: Any) -> Any:
    """Coerce SQLAlchemy / stdlib types to something ``JSONB`` accepts."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    # Enum with .value / anything else — fall back to string.
    inner = getattr(value, "value", None)
    if inner is not None and isinstance(inner, (str, int)):
        return inner
    return str(value)


def diff_from_models(before: Any, after: Any, fields: list[str]) -> dict[str, Any]:
    """Build a ``{"before": {...}, "after": {...}}`` diff of changed fields.

    Only fields whose value actually changed appear in the output.
    Missing attributes map to ``None`` so create/delete-adjacent callers
    can pass ``before=None`` or ``after=None`` without surprises.

    Non-JSON-serialisable values (``UUID``, ``datetime``, ``Decimal``,
    enums) are coerced to strings via :func:`_json_safe`.
    """
    before_out: dict[str, Any] = {}
    after_out: dict[str, Any] = {}
    for field in fields:
        b = _json_safe(getattr(before, field, None)) if before is not None else None
        a = _json_safe(getattr(after, field, None)) if after is not None else None
        if b != a:
            before_out[field] = b
            after_out[field] = a
    if not before_out and not after_out:
        return {}
    return {"before": before_out, "after": after_out}


# ---------------------------------------------------------------------------
# Writing events
# ---------------------------------------------------------------------------


async def record(
    db: AsyncSession,
    *,
    action: str,
    entity_type: str,
    entity_id: UUID,
    entity_label: str,
    actor: ActorInfo,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    diff: dict[str, Any] | None = None,
    request_id: str | None = None,
    tenant_id: UUID | None = None,
) -> AuditEvent:
    """Append an :class:`AuditEvent` row in the caller's transaction.

    Does **not** commit — the surrounding service already owns the
    transaction, and atomicity is part of the contract (if the outer
    commit rolls back, the audit row must disappear too).

    ``tenant_id`` can be omitted when the session already has an RLS
    context set via ``SET LOCAL app.tenant_id`` — in that case we read
    it from Postgres so callers don't have to thread it through. The
    app's ``get_db`` dependency sets this for every request.
    """
    if tenant_id is None:
        from sqlalchemy import text

        row = (await db.execute(text("SELECT current_setting('app.tenant_id', true)"))).scalar()
        if not row:
            raise RuntimeError(
                "audit_service.record: no app.tenant_id in session and no "
                "tenant_id argument — cannot attribute the event."
            )
        tenant_id = UUID(str(row))

    if diff is None:
        payload: dict[str, Any] | None
        if before is None and after is None:
            payload = None
        else:
            payload = {
                "before": _json_safe(before) if before is not None else None,
                "after": _json_safe(after) if after is not None else None,
            }
    else:
        payload = _json_safe(diff)

    event = AuditEvent(
        tenant_id=tenant_id,
        occurred_at=datetime.now(UTC),
        actor_type=actor.type,
        actor_id=actor.id,
        actor_label=actor.label[:255],
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_label=str(entity_label)[:255],
        diff=payload,
        request_id=request_id,
    )
    db.add(event)
    await db.flush()
    return event


# ---------------------------------------------------------------------------
# Reading events
# ---------------------------------------------------------------------------


def _apply_principal_scope(stmt: Select, principal: Any) -> Select:
    """Restrict reads to what the caller is allowed to see.

    Staff (``is_staff=True``) see every event in the tenant. Customer
    contacts see only ``entity_type='order'`` events whose ``entity_id``
    points at one of their own customer's orders.
    """
    is_staff = bool(getattr(principal, "is_staff", False))
    if is_staff:
        return stmt

    customer_id = getattr(principal, "customer_id", None)
    if customer_id is None:
        # Contact without a customer should not see anything.
        return stmt.where(false())

    # Subquery: order IDs belonging to the contact's customer.
    order_ids = select(Order.id).where(Order.customer_id == customer_id)
    return stmt.where(
        and_(
            AuditEvent.entity_type == "order",
            AuditEvent.entity_id.in_(order_ids),
        )
    )


async def list_events(
    db: AsyncSession,
    *,
    principal: Any,
    entity_type: str | None = None,
    entity_id: UUID | None = None,
    actor_id: UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    q: str | None = None,
    exclude_actions: list[str] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[AuditEvent], int]:
    """Return ``(events, total_count)`` matching the filters.

    Results are newest-first. The tenant gate is RLS — every query on
    ``db`` is already scoped by the session's ``app.tenant_id``. Layered
    on top: staff see everything in that tenant, contacts see only
    ``order`` events for their own customer (see
    :func:`_apply_principal_scope`).
    """
    stmt = select(AuditEvent).order_by(AuditEvent.occurred_at.desc())
    stmt = _apply_principal_scope(stmt, principal)

    if entity_type:
        stmt = stmt.where(AuditEvent.entity_type == entity_type)
    if entity_id is not None:
        stmt = stmt.where(AuditEvent.entity_id == entity_id)
    if actor_id is not None:
        stmt = stmt.where(AuditEvent.actor_id == actor_id)
    if date_from is not None:
        stmt = stmt.where(AuditEvent.occurred_at >= date_from)
    if date_to is not None:
        from datetime import timedelta

        stmt = stmt.where(AuditEvent.occurred_at < date_to + timedelta(days=1))
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                AuditEvent.action.ilike(pattern),
                AuditEvent.entity_label.ilike(pattern),
                AuditEvent.actor_label.ilike(pattern),
            )
        )
    if exclude_actions:
        stmt = stmt.where(AuditEvent.action.notin_(exclude_actions))

    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = int((await db.execute(count_stmt)).scalar() or 0)

    stmt = stmt.offset(max(0, offset)).limit(max(1, min(limit, 200)))
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows), total


async def list_recent(
    db: AsyncSession,
    *,
    principal: Any,
    limit: int = 20,
) -> list[AuditEvent]:
    """Return the N most recent audit events visible to ``principal``.

    Thin convenience wrapper over :func:`list_events` for the dashboard
    "Recent activity" widget (§7). Reuses the same scoping rules — staff
    see everything in the tenant, contacts see only ``order`` events on
    their own customer's orders — and the same RLS session guarantee.

    Auth events (``auth.login``, ``auth.logout``) are excluded — they
    belong in the full audit log at ``/app/admin/audit`` for forensics,
    but cluttering the dashboard activity widget with "Alice logged in"
    every few minutes obscures real business events.
    """
    events, _ = await list_events(
        db,
        principal=principal,
        limit=limit,
        offset=0,
        exclude_actions=["auth.login", "auth.logout"],
    )
    return events
