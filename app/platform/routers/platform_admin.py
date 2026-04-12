"""Platform admin: CRUD tenants, basic oversight.

Gated by `require_platform_admin`, so a regular Identity cannot see it.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform.deps import get_platform_db, require_platform_admin
from app.platform.models import Identity
from app.platform.service import (
    DuplicateTenantSlug,
    PlatformError,
    create_tenant_with_owner,
    deactivate_tenant,
    list_tenants,
)
from app.security.csrf import verify_csrf

router = APIRouter(
    prefix="/platform/admin",
    tags=["platform-admin"],
    dependencies=[Depends(verify_csrf)],
)


def _templates(request: Request):
    return request.app.state.templates


@router.get("/tenants", response_class=HTMLResponse)
async def tenants_index(
    request: Request,
    identity: Identity = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_platform_db),
) -> HTMLResponse:
    tenants = await list_tenants(db)
    html = _templates(request).render(
        request,
        "platform/admin/tenants.html",
        {
            "identity": identity,
            "tenants": tenants,
            "error": None,
            "notice": None,
            "principal": None,
        },
    )
    return HTMLResponse(html)


@router.post("/tenants", response_class=HTMLResponse)
async def tenants_create(
    request: Request,
    slug: str = Form(...),
    name: str = Form(...),
    owner_email: str = Form(...),
    owner_full_name: str = Form(...),
    owner_password: str = Form(...),
    identity: Identity = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_platform_db),
) -> Response:
    try:
        await create_tenant_with_owner(
            db,
            slug=slug,
            name=name,
            owner_email=owner_email,
            owner_full_name=owner_full_name,
            owner_password=owner_password,
        )
    except DuplicateTenantSlug:
        tenants = await list_tenants(db)
        html = _templates(request).render(
            request,
            "platform/admin/tenants.html",
            {
                "identity": identity,
                "tenants": tenants,
                "error": f"Tenant se slugem '{slug}' už existuje.",
                "notice": None,
                "principal": None,
            },
        )
        return HTMLResponse(html, status_code=400)
    except PlatformError as exc:
        tenants = await list_tenants(db)
        html = _templates(request).render(
            request,
            "platform/admin/tenants.html",
            {
                "identity": identity,
                "tenants": tenants,
                "error": str(exc),
                "notice": None,
                "principal": None,
            },
        )
        return HTMLResponse(html, status_code=400)

    await db.commit()
    return RedirectResponse(url="/platform/admin/tenants", status_code=303)


@router.post("/tenants/{tenant_id}/deactivate")
async def tenants_deactivate(
    tenant_id: UUID,
    identity: Identity = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_platform_db),
) -> Response:
    try:
        await deactivate_tenant(db, tenant_id=tenant_id)
    except PlatformError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    await db.commit()
    return RedirectResponse(url="/platform/admin/tenants", status_code=303)
