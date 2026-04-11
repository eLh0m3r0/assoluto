"""Dashboard route — the rendez-vous point after login.

Tenant staff see counts of customers / orders / assets.
Customer contacts see a minimalist welcome placeholder until M2 adds
their own order list.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import Principal, get_db, require_login
from app.models.customer import Customer

router = APIRouter(prefix="/app", tags=["dashboard"])


def _templates(request: Request):
    return request.app.state.templates


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard_index(
    request: Request,
    principal: Principal = Depends(require_login),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    tenant = getattr(request.state, "tenant", None)
    if tenant is None:
        raise HTTPException(status_code=500, detail="Tenant not resolved")

    stats: dict[str, int] = {}
    if principal.is_staff:
        count = await db.execute(select(func.count()).select_from(Customer))
        stats["customers"] = int(count.scalar() or 0)

    html = _templates(request).render(
        request,
        "dashboard/index.html",
        {
            "principal": principal,
            "tenant": tenant,
            "stats": stats,
        },
    )
    return HTMLResponse(html)
