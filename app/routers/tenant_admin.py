"""Tenant admin routes: staff user management + self-service profile."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import Principal, get_db, require_tenant_staff
from app.i18n import t as _t
from app.models.enums import UserRole
from app.models.user import User
from app.security.csrf import verify_csrf
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


# Re-export InvalidInvitation so other modules can pretend this router
# owns the staff invite flow even though the service layer does the work.
__all__ = ["InvalidInvitation", "router"]
