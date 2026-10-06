"""Where a public-demo visitor should look first (P2-14).

The seed (:mod:`app.demo.seed`) decides *what* exists; this module only
finds the showcase rows by their shape, so it keeps working whatever the
seed's numbers, titles or dates are:

* **flagship quote** — the most recent order waiting in ``QUOTED`` (the
  one with a drawing, a price and a "Confirm" button);
* **overdue order** — the in-production order whose promised date passed
  longest ago;
* **customer material** — the first active asset.

All lookups run on the request's RLS-scoped session, so they can only
ever see the current tenant. Callers use them only when
``request.state.public_demo`` is set.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset
from app.models.enums import OrderStatus
from app.models.order import Order

#: Accounting export page (POHODA / Money S3) — staff admin only.
EXPORTS_URL = "/app/admin/exports"


async def flagship_quote(db: AsyncSession, *, customer_id: UUID | None = None) -> Order | None:
    """Most recent ``QUOTED`` order (optionally of one client)."""
    stmt = select(Order).where(Order.status == OrderStatus.QUOTED)
    if customer_id is not None:
        stmt = stmt.where(Order.customer_id == customer_id)
    stmt = stmt.order_by(Order.created_at.desc(), Order.number.desc()).limit(1)
    return (await db.execute(stmt)).scalars().first()


async def overdue_order(db: AsyncSession, *, today: date) -> Order | None:
    """In-production order with the oldest promised date before ``today``."""
    stmt = (
        select(Order)
        .where(
            Order.status == OrderStatus.IN_PRODUCTION,
            Order.promised_delivery_at.is_not(None),
            Order.promised_delivery_at < today,
        )
        .order_by(Order.promised_delivery_at.asc(), Order.number.asc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first()


async def first_material(db: AsyncSession) -> Asset | None:
    """The first active customer-material record."""
    stmt = (
        select(Asset)
        .where(Asset.is_active.is_(True))
        .order_by(Asset.created_at.asc(), Asset.code.asc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first()


async def start_here_links(db: AsyncSession, *, today: date) -> dict[str, Any]:
    """Rows for the supplier dashboard's "Where to start" card.

    Missing rows come back as ``None``; the template skips them.
    """
    return {
        "quote": await flagship_quote(db),
        "overdue": await overdue_order(db, today=today),
        "material": await first_material(db),
        "exports_url": EXPORTS_URL,
    }
