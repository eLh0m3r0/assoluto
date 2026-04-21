"""Global search route — powers the ⌘K command palette.

Returns an HTML fragment meant to be swapped into the palette modal via
HTMX (`hx-target="#palette-results"`, `hx-swap="innerHTML"`). Both staff
and customer-contact principals can hit this endpoint; scoping is
enforced inside `search_service.global_search`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import Principal, get_db, require_login
from app.security.csrf import verify_csrf
from app.services.search_service import global_search

router = APIRouter(prefix="/app", tags=["search"], dependencies=[Depends(verify_csrf)])


def _templates(request: Request):
    return request.app.state.templates


@router.get("/search", response_class=HTMLResponse)
async def palette_search(
    request: Request,
    q: str = "",
    principal: Principal = Depends(require_login),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Render the command-palette results fragment for `q`.

    Queries shorter than 2 characters yield an empty fragment (no 400);
    this lets the client send `keyup` events unconditionally without
    special-casing short inputs in JS. The service honours RLS and
    per-customer scoping for contact principals.
    """
    results = await global_search(db, principal=principal, q=q)

    html = _templates(request).render(
        request,
        "_palette_results.html",
        {
            "principal": principal,
            "results": results,
            "query": q.strip(),
        },
    )
    return HTMLResponse(html)
