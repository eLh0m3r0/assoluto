"""Tenant admin routes: staff user management + self-service profile."""

from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import Principal, get_db, require_tenant_staff
from app.i18n import t as _t
from app.models.enums import UserRole
from app.models.user import User
from app.security.csrf import verify_csrf
from app.services import sla_service
from app.services.audit_service import actor_from_principal
from app.services.auth_service import (
    InvalidCredentials,
    InvalidInvitation,
    change_user_password,
    create_staff_invite_token,
    invite_tenant_staff,
)
from app.tasks.email_tasks import send_staff_invitation

router = APIRouter(prefix="/app/admin", tags=["tenant-admin"], dependencies=[Depends(verify_csrf)])


def _templates(request: Request):
    return request.app.state.templates


def _tenant(request: Request):
    tenant = getattr(request.state, "tenant", None)
    if tenant is None:
        raise HTTPException(status_code=500, detail="Tenant not resolved")
    return tenant


def _require_tenant_admin(principal: Principal) -> None:
    if principal.role != UserRole.TENANT_ADMIN.value:
        raise HTTPException(
            status_code=403,
            detail="Tenant admin required",
        )


# --------------------------------------------------------------- users


@router.get("/users", response_class=HTMLResponse)
async def users_index(
    request: Request,
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    _require_tenant_admin(principal)
    result = await db.execute(select(User).order_by(User.full_name))
    users = list(result.scalars().all())
    html = _templates(request).render(
        request,
        "admin/users.html",
        {
            "principal": principal,
            "tenant": _tenant(request),
            "users": users,
            "error": None,
            "notice": None,
        },
    )
    return HTMLResponse(html)


@router.post("/users/invite", response_class=HTMLResponse)
async def users_invite(
    request: Request,
    background_tasks: BackgroundTasks,
    email: str = Form(...),
    full_name: str = Form(...),
    role: str = Form("tenant_staff"),
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> Response:
    _require_tenant_admin(principal)

    try:
        role_enum = UserRole(role)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid role") from None

    # Explicit uniqueness check BEFORE insert — keeps the session alive
    # so we can re-render the page on conflict. Relying on IntegrityError
    # would taint the open transaction.
    normalized_email = email.strip().lower()
    dup = (
        await db.execute(select(User).where(User.email == normalized_email))
    ).scalar_one_or_none()
    if dup is not None:
        result = await db.execute(select(User).order_by(User.full_name))
        users = list(result.scalars().all())
        html = _templates(request).render(
            request,
            "admin/users.html",
            {
                "principal": principal,
                "tenant": _tenant(request),
                "users": users,
                "error": _t(request, "A user with this email already exists."),
                "notice": None,
            },
        )
        return HTMLResponse(html, status_code=400)

    user = await invite_tenant_staff(
        db,
        tenant_id=principal.tenant_id,
        email=email,
        full_name=full_name,
        role=role_enum,
        audit_actor=actor_from_principal(principal),
    )

    # Explicit commit before scheduling the email task — the background
    # task's fresh session must see the just-written User row. See
    # CLAUDE.md "BackgroundTasks + explicit commit".
    await db.commit()
    settings = request.app.state.settings
    token = create_staff_invite_token(
        settings.app_secret_key,
        tenant_id=principal.tenant_id,
        user_id=user.id,
    )
    sender = request.app.state.email_sender
    tenant = _tenant(request)
    from app.urls import tenant_base_url

    invite_url = f"{tenant_base_url(settings, tenant)}/invite/staff?token={token}"
    background_tasks.add_task(
        send_staff_invitation,
        sender,
        to=user.email,
        tenant_name=tenant.name,
        invitee_name=user.full_name,
        invite_url=invite_url,
    )

    return RedirectResponse(url="/app/admin/users", status_code=303)


@router.post("/users/{user_id}/disable", response_class=HTMLResponse)
async def users_disable(
    user_id: UUID,
    request: Request,
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> Response:
    _require_tenant_admin(principal)

    target = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == principal.id:
        raise HTTPException(status_code=400, detail="Cannot disable yourself")
    target.is_active = False
    target.session_version += 1  # kick out existing sessions
    await db.flush()
    return RedirectResponse(url="/app/admin/users", status_code=303)


# -------------------------------------------------------------- profile


@router.get("/profile", response_class=HTMLResponse)
async def profile_form(
    request: Request,
    saved: int = 0,
    principal: Principal = Depends(require_tenant_staff),
) -> HTMLResponse:
    html = _templates(request).render(
        request,
        "admin/profile.html",
        {
            "principal": principal,
            "tenant": _tenant(request),
            "error": None,
            "notice": "Profil uložen." if saved else None,
        },
    )
    return HTMLResponse(html)


@router.post("/profile", response_class=HTMLResponse)
async def profile_update(
    request: Request,
    full_name: str = Form(...),
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> Response:
    full_name = (full_name or "").strip()
    if not full_name:
        html = _templates(request).render(
            request,
            "admin/profile.html",
            {
                "principal": principal,
                "tenant": _tenant(request),
                "error": _t(request, "Name cannot be empty."),
                "notice": None,
            },
        )
        return HTMLResponse(html, status_code=400)
    user = (await db.execute(select(User).where(User.id == principal.id))).scalar_one()
    user.full_name = full_name
    await db.flush()
    await db.commit()
    # Redirect so the header badge picks up the new name on its own render.
    return RedirectResponse(url="/app/admin/profile?saved=1", status_code=303)


@router.post("/profile/password", response_class=HTMLResponse)
async def profile_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    new_password_confirm: str = Form(...),
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> Response:
    if new_password != new_password_confirm:
        html = _templates(request).render(
            request,
            "admin/profile.html",
            {
                "principal": principal,
                "tenant": _tenant(request),
                "error": _t(request, "New passwords do not match."),
                "notice": None,
            },
        )
        return HTMLResponse(html, status_code=400)

    user = (await db.execute(select(User).where(User.id == principal.id))).scalar_one()

    try:
        await change_user_password(
            db,
            user=user,
            current_password=current_password,
            new_password=new_password,
            audit_actor=actor_from_principal(principal),
        )
    except InvalidCredentials as exc:
        html = _templates(request).render(
            request,
            "admin/profile.html",
            {
                "principal": principal,
                "tenant": _tenant(request),
                "error": str(exc),
                "notice": None,
            },
        )
        return HTMLResponse(html, status_code=400)

    # session_version got bumped — the current cookie is now stale. Force
    # a fresh login.
    response = RedirectResponse(url="/auth/login", status_code=303)
    from app.security.session import clear_session

    clear_session(response)
    return response


# --------------------------------------------------------------- audit log


AUDIT_PAGE_SIZE = 50

# Known entity_type values rendered in the filter dropdown. Kept tight
# so typos in the URL don't clutter the UI — unknown types still work
# via direct query-string access.
AUDIT_ENTITY_CHOICES: tuple[tuple[str, str], ...] = (
    ("", "All entities"),
    ("order", "Order"),
    ("customer", "Client"),
    ("product", "Product"),
    ("user", "User"),
)


@router.get("/audit", response_class=HTMLResponse)
async def audit_index(
    request: Request,
    entity_type: str | None = None,
    actor_id: str | None = None,
    from_: str | None = Query(None, alias="from"),
    to: str | None = None,
    q: str | None = None,
    page: int = 1,
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Render the audit-log viewer (staff-only)."""
    from app.services.audit_service import list_events

    entity_type_value = (entity_type or "").strip() or None
    actor_uuid: UUID | None = None
    if actor_id and actor_id.strip():
        try:
            actor_uuid = UUID(actor_id.strip())
        except ValueError:
            actor_uuid = None

    date_from: date | None = None
    if from_ and from_.strip():
        try:
            date_from = date.fromisoformat(from_.strip())
        except ValueError:
            date_from = None
    date_to: date | None = None
    if to and to.strip():
        try:
            date_to = date.fromisoformat(to.strip())
        except ValueError:
            date_to = None

    page = max(1, page)
    offset = (page - 1) * AUDIT_PAGE_SIZE

    events, total = await list_events(
        db,
        principal=principal,
        entity_type=entity_type_value,
        actor_id=actor_uuid,
        date_from=date_from,
        date_to=date_to,
        q=(q or None),
        limit=AUDIT_PAGE_SIZE,
        offset=offset,
    )
    total_pages = max(1, (total + AUDIT_PAGE_SIZE - 1) // AUDIT_PAGE_SIZE)

    html = _templates(request).render(
        request,
        "admin/audit.html",
        {
            "principal": principal,
            "tenant": _tenant(request),
            "events": events,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "filters": {
                "entity_type": entity_type_value or "",
                "actor_id": actor_id or "",
                "from": from_ or "",
                "to": to or "",
                "q": q or "",
            },
            "entity_choices": AUDIT_ENTITY_CHOICES,
        },
    )
    return HTMLResponse(html)


# ----------------------------------------------------------------- SLA


_SLA_TIMEFRAMES = {"30": 30, "90": 90, "365": 365}


def _heatmap_grid(cells: list[dict]) -> dict:
    """Pivot the flat cell list from ``sla_service.heatmap_data`` into a
    grid suitable for rendering."""
    weeks: list = []
    seen_weeks: set = set()
    rows_by_customer: dict[UUID, dict] = {}
    for cell in cells:
        ws = cell["week_start"]
        if ws not in seen_weeks:
            seen_weeks.add(ws)
            weeks.append(ws)
        cid = cell["customer_id"]
        row = rows_by_customer.get(cid)
        if row is None:
            row = {
                "customer_id": cid,
                "customer_name": cell["customer_name"],
                "cells": {},
            }
            rows_by_customer[cid] = row
        total = cell["total"]
        ratio = (cell["on_time"] / total) if total > 0 else None
        row["cells"][ws] = {
            "on_time": cell["on_time"],
            "late": cell["late"],
            "total": total,
            "ratio": ratio,
        }
    weeks.sort()
    rows = sorted(rows_by_customer.values(), key=lambda r: r["customer_name"].lower())
    return {"weeks": weeks, "rows": rows}


@router.get("/sla", response_class=HTMLResponse)
async def sla_dashboard(
    request: Request,
    timeframe: str = Query("90"),
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """On-time delivery summary + per-customer weekly heatmap."""
    import traceback

    days = _SLA_TIMEFRAMES.get(timeframe, 90)
    timeframe_value = timeframe if timeframe in _SLA_TIMEFRAMES else "90"

    today = date.today()
    date_from = today - timedelta(days=days)

    try:
        summary = await sla_service.on_time_rate(db, date_from=date_from, date_to=today)
        heatmap_weeks = max(8, min(52, (days // 7) + 1))
        cells = await sla_service.heatmap_data(db, weeks=heatmap_weeks)
        grid = _heatmap_grid(cells)

        html = _templates(request).render(
            request,
            "admin/sla.html",
            {
                "principal": principal,
                "tenant": _tenant(request),
                "summary": summary,
                "rate_pct": round(summary["rate"] * 100, 1),
                "grid": grid,
                "timeframe": timeframe_value,
                "timeframes": list(_SLA_TIMEFRAMES.keys()),
                "date_from": date_from,
                "date_to": today,
            },
        )
        return HTMLResponse(html)
    except Exception as exc:
        return HTMLResponse(
            f"<pre style='padding:1rem;font:12px monospace;white-space:pre-wrap'>"
            f"SLA DEBUG — {exc!r}\n\n{traceback.format_exc()}</pre>",
            status_code=500,
        )


# Re-export InvalidInvitation so other modules can pretend this router
# owns the staff invite flow even though the service layer does the work.
__all__ = ["InvalidInvitation", "router"]
