"""Activation funnel for the platform-admin dashboard (BIZ-09, BIZ-16).

A signup is not a customer. The number that predicts conversion for a
supplier→customer portal is whether the supplier got *their* customer to
use it. So the funnel counts, per signup week:

1. **verified signups** — identities that confirmed their email. Bots
   never do (every one of the 15 August/September signups stayed
   unverified), so unverified identities are shown as a separate
   "excluded" number and never enter the denominator;
2. **tenant created** — the identity holds a staff membership;
3. **first customer invited** — that tenant has at least one customer
   contact row;
4. **first contact login** — one of those contacts accepted the
   invitation or signed in;
5. **first order by a contact** — an order with ``created_by_contact_id``.

All counts are aggregates over the owner session (platform admin is an
operator role: aggregate metrics are allowed, tenant business data is
not — CLAUDE.md §6). Nothing here reads names, orders or comments.

Server-side only, no cookies, no third-party analytics.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _(msg: str) -> str:
    """Msgid marker for ``pybabel extract``; the template translates."""
    return msg


#: Funnel stages, in order, as (attribute, English label msgid). The
#: template renders ``_(label)``.
FUNNEL_STAGES: tuple[tuple[str, str], ...] = (
    ("verified", _("Verified signups")),
    ("tenant_created", _("Portal created")),
    ("customer_invited", _("First customer invited")),
    ("contact_logged_in", _("First customer login")),
    ("contact_ordered", _("First order by a customer")),
)


@dataclass(frozen=True)
class FunnelWeek:
    week_start: date
    unverified: int
    verified: int
    tenant_created: int
    customer_invited: int
    contact_logged_in: int
    contact_ordered: int

    def stage(self, name: str) -> int:
        return int(getattr(self, name))


# Identity → its staff tenants → activation flags. An identity that only
# holds *contact* memberships (a customer who was linked to the platform
# login) is not a signup and is left out; an identity with no membership
# at all is still a signup that never got a tenant.
_FUNNEL_SQL = text(
    """
    SELECT date_trunc('week', i.created_at)::date AS wk,
           count(*) FILTER (WHERE i.email_verified_at IS NULL) AS unverified,
           count(*) FILTER (WHERE i.email_verified_at IS NOT NULL) AS verified,
           count(*) FILTER (WHERE i.email_verified_at IS NOT NULL
                              AND a.has_tenant) AS tenant_created,
           count(*) FILTER (WHERE i.email_verified_at IS NOT NULL
                              AND a.invited) AS customer_invited,
           count(*) FILTER (WHERE i.email_verified_at IS NOT NULL
                              AND a.logged_in) AS contact_logged_in,
           count(*) FILTER (WHERE i.email_verified_at IS NOT NULL
                              AND a.ordered) AS contact_ordered
    FROM platform_identities i
    LEFT JOIN LATERAL (
        SELECT bool_or(true) AS has_tenant,
               bool_or(EXISTS (
                   SELECT 1 FROM customer_contacts cc WHERE cc.tenant_id = m.tenant_id
               )) AS invited,
               bool_or(EXISTS (
                   SELECT 1 FROM customer_contacts cc
                   WHERE cc.tenant_id = m.tenant_id
                     AND (cc.last_login_at IS NOT NULL OR cc.accepted_at IS NOT NULL)
               )) AS logged_in,
               bool_or(EXISTS (
                   SELECT 1 FROM orders o
                   WHERE o.tenant_id = m.tenant_id AND o.created_by_contact_id IS NOT NULL
               )) AS ordered
        FROM platform_tenant_memberships m
        WHERE m.identity_id = i.id
          AND m.access_type = 'member'
          AND m.user_id IS NOT NULL
    ) a ON true
    WHERE i.is_platform_admin = false
      AND i.created_at >= :since
      AND (
          a.has_tenant
          OR NOT EXISTS (
              SELECT 1 FROM platform_tenant_memberships mc
              WHERE mc.identity_id = i.id AND mc.contact_id IS NOT NULL
          )
      )
    GROUP BY wk
    """
)


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


async def weekly_funnel(
    db: AsyncSession, *, weeks: int = 12, now: datetime | None = None
) -> list[FunnelWeek]:
    """Return one :class:`FunnelWeek` per ISO week, newest first.

    Weeks without any signup are included as zero rows so the table
    reads as a continuous timeline.
    """
    current = now or datetime.now(UTC)
    first_week = _week_start(current.date()) - timedelta(weeks=weeks - 1)
    since = datetime.combine(first_week, datetime.min.time(), tzinfo=UTC)
    rows = (await db.execute(_FUNNEL_SQL, {"since": since})).mappings().all()
    by_week = {row["wk"]: row for row in rows}
    out: list[FunnelWeek] = []
    for offset in range(weeks):
        wk = first_week + timedelta(weeks=weeks - 1 - offset)
        row = by_week.get(wk)
        out.append(
            FunnelWeek(
                week_start=wk,
                unverified=int(row["unverified"]) if row else 0,
                verified=int(row["verified"]) if row else 0,
                tenant_created=int(row["tenant_created"]) if row else 0,
                customer_invited=int(row["customer_invited"]) if row else 0,
                contact_logged_in=int(row["contact_logged_in"]) if row else 0,
                contact_ordered=int(row["contact_ordered"]) if row else 0,
            )
        )
    return out


def funnel_totals(weeks: list[FunnelWeek]) -> FunnelWeek:
    """Sum of every week — the "whole window" row of the table."""
    return FunnelWeek(
        week_start=weeks[-1].week_start if weeks else date.today(),
        unverified=sum(w.unverified for w in weeks),
        verified=sum(w.verified for w in weeks),
        tenant_created=sum(w.tenant_created for w in weeks),
        customer_invited=sum(w.customer_invited for w in weeks),
        contact_logged_in=sum(w.contact_logged_in for w in weeks),
        contact_ordered=sum(w.contact_ordered for w in weeks),
    )


@dataclass(frozen=True)
class TenantActivation:
    slug: str
    name: str
    created_at: datetime
    is_active: bool
    #: True / False for a signup-created tenant; None when the tenant has
    #: no member identity at all (created by a script before the platform
    #: layer existed).
    owner_verified: bool | None
    contacts_invited: int
    contacts_active_30d: int
    contact_orders_30d: int
    orders_30d: int


_TENANT_ACTIVATION_SQL = text(
    """
    SELECT t.slug, t.name, t.created_at, t.is_active,
           (SELECT bool_or(i.email_verified_at IS NOT NULL)
              FROM platform_tenant_memberships m
              JOIN platform_identities i ON i.id = m.identity_id
             WHERE m.tenant_id = t.id AND m.access_type = 'member'
               AND m.user_id IS NOT NULL) AS owner_verified,
           (SELECT count(*) FROM customer_contacts cc
             WHERE cc.tenant_id = t.id) AS contacts_invited,
           (SELECT count(*) FROM customer_contacts cc
             WHERE cc.tenant_id = t.id AND cc.last_login_at >= :since) AS contacts_active_30d,
           (SELECT count(*) FROM orders o
             WHERE o.tenant_id = t.id AND o.created_by_contact_id IS NOT NULL
               AND o.created_at >= :since) AS contact_orders_30d,
           (SELECT count(*) FROM orders o
             WHERE o.tenant_id = t.id AND o.created_at >= :since) AS orders_30d
    FROM tenants t
    ORDER BY t.created_at DESC
    LIMIT :limit
    """
)


async def tenant_activation(
    db: AsyncSession, *, limit: int = 50, now: datetime | None = None
) -> list[TenantActivation]:
    """Per-tenant activation columns (BIZ-16 #4), newest tenants first."""
    current = now or datetime.now(UTC)
    rows = (
        (
            await db.execute(
                _TENANT_ACTIVATION_SQL,
                {"since": current - timedelta(days=30), "limit": limit},
            )
        )
        .mappings()
        .all()
    )
    return [
        TenantActivation(
            slug=row["slug"],
            name=row["name"],
            created_at=row["created_at"],
            is_active=bool(row["is_active"]),
            owner_verified=row["owner_verified"],
            contacts_invited=int(row["contacts_invited"]),
            contacts_active_30d=int(row["contacts_active_30d"]),
            contact_orders_30d=int(row["contact_orders_30d"]),
            orders_30d=int(row["orders_30d"]),
        )
        for row in rows
    ]


async def signup_refs(db: AsyncSession, *, days: int = 90, now: datetime | None = None) -> dict:
    """Count signups attributed to the "Powered by" footer (MKT-9).

    The ref is stored at signup in ``tenants.settings["signup_ref"]``;
    returns ``{"portal": n, ...}`` over the last ``days`` days, keyed by
    the ``ref`` value.
    """
    current = now or datetime.now(UTC)
    rows = (
        await db.execute(
            text(
                "SELECT settings->'signup_ref'->>'ref' AS ref, count(*) AS n "
                "FROM tenants "
                "WHERE created_at >= :since AND settings->'signup_ref' IS NOT NULL "
                "GROUP BY 1"
            ),
            {"since": current - timedelta(days=days)},
        )
    ).all()
    return {ref: int(n) for ref, n in rows if ref}
