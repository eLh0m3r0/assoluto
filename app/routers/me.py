"""Customer-contact self-service profile.

Companion to ``app/routers/tenant_admin.py``'s ``/app/admin/profile`` —
that one is staff-only. Customer contacts (the people invited by a
manufacturer to view their own orders) need the same self-service
surface but mounted somewhere they can reach.

Mounted at ``/app/me/*``. Uses ``require_login`` so a misconfigured
session can't slip through, but only renders the contact branch — staff
who land here are bounced to the staff equivalent.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.deps import Principal, get_db, require_login
from app.i18n import supported_locale_list
from app.i18n import t as _t
from app.models.customer import CustomerContact
from app.security.csrf import verify_csrf
from app.services.audit_service import actor_from_principal
from app.services.auth_service import (
    InvalidCredentials,
    change_contact_password,
)

router = APIRouter(prefix="/app/me", tags=["me"], dependencies=[Depends(verify_csrf)])


def _templates(request: Request):
    return request.app.state.templates


def _tenant(request: Request):
    tenant = getattr(request.state, "tenant", None)
    if tenant is None:
        raise HTTPException(status_code=500, detail="Tenant not resolved")
    return tenant


def _ensure_contact_or_redirect(principal: Principal) -> RedirectResponse | None:
    """Return a redirect for staff (they belong on /app/admin/profile),
    or None for contacts (which is who this router is for).

    The previous version raised ``HTTPException(303, ...)`` which fell
    through the JSON error handler and emitted ``content-type:
    application/json`` on a 303. Browsers followed it fine but it was an
    untidy response shape.
    """
    if principal.is_staff:
        return RedirectResponse(url="/app/admin/profile", status_code=303)
    return None


def _normalise_locale(raw: str) -> str | None:
    """Normalise a locale code from form input, gating against the
    currently-configured ``SUPPORTED_LOCALES``. Reading the env on each
    call (rather than freezing at import) means a deploy that toggles
    the locale list takes effect without a full reload."""
    code = (raw or "").strip().lower().split("-", 1)[0]
    supported = set(supported_locale_list(get_settings().supported_locales))
    return code if code in supported else None


@router.get("/profile", response_class=HTMLResponse)
async def profile_form(
    request: Request,
    saved: int = 0,
    error: str | None = None,
    pwsaved: int = 0,
    principal: Principal = Depends(require_login),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    redirect = _ensure_contact_or_redirect(principal)
    if redirect is not None:
        return redirect
    contact = (
        await db.execute(select(CustomerContact).where(CustomerContact.id == principal.id))
    ).scalar_one()
    notice = None
    if saved:
        notice = _t(request, "Profile saved.")
    elif pwsaved:
        notice = _t(request, "Password changed. Sign in again with your new password.")
    html = _templates(request).render(
        request,
        "me/profile.html",
        {
            "principal": principal,
            "tenant": _tenant(request),
            "contact": contact,
            "preferred_locale": contact.preferred_locale,
            "error": error,
            "notice": notice,
        },
    )
    return HTMLResponse(html)


@router.post("/profile", response_class=HTMLResponse)
async def profile_update(
    request: Request,
    full_name: str = Form(...),
    preferred_locale: str = Form(""),
    principal: Principal = Depends(require_login),
    db: AsyncSession = Depends(get_db),
) -> Response:
    redirect = _ensure_contact_or_redirect(principal)
    if redirect is not None:
        return redirect
    cleaned_name = (full_name or "").strip()
    if not cleaned_name:
        return RedirectResponse(
            url="/app/me/profile?error=" + quote(_t(request, "Name cannot be empty.")),
            status_code=303,
        )
    contact = (
        await db.execute(select(CustomerContact).where(CustomerContact.id == principal.id))
    ).scalar_one()
    contact.full_name = cleaned_name
    contact.preferred_locale = _normalise_locale(preferred_locale)
    await db.flush()
    await db.commit()
    return RedirectResponse(url="/app/me/profile?saved=1", status_code=303)


@router.post("/profile/password", response_class=HTMLResponse)
async def profile_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    new_password_confirm: str = Form(...),
    principal: Principal = Depends(require_login),
    db: AsyncSession = Depends(get_db),
) -> Response:
    redirect = _ensure_contact_or_redirect(principal)
    if redirect is not None:
        return redirect
    if new_password != new_password_confirm:
        return RedirectResponse(
            url="/app/me/profile?error=" + quote(_t(request, "New passwords do not match.")),
            status_code=303,
        )
    contact = (
        await db.execute(select(CustomerContact).where(CustomerContact.id == principal.id))
    ).scalar_one()
    try:
        await change_contact_password(
            db,
            contact=contact,
            current_password=current_password,
            new_password=new_password,
            audit_actor=actor_from_principal(principal),
        )
    except InvalidCredentials as exc:
        return RedirectResponse(
            url="/app/me/profile?error=" + quote(str(exc)),
            status_code=303,
        )
    # session_version got bumped — current cookie is stale. Force re-auth.
    response = RedirectResponse(url="/auth/login?notice=password_reset", status_code=303)
    from app.security.session import clear_session

    clear_session(response)
    return response
