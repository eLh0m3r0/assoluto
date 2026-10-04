"""GDPR self-service for the platform Identity (audit SEC-2 / F-12).

The paying tenant owner is Assoluto's own data subject (e-mail, name,
password hash, consent IP), but only the tenant-side ``User`` /
``CustomerContact`` rows had export and erasure routes. These routes
close the gap for the logged-in Identity:

* ``GET  /platform/profile`` — the page with both actions.
* ``GET  /platform/profile/export`` — Art. 15 / 20: one JSON file with
  the Identity, its consent record, every membership and the per-tenant
  record of each staff / contact membership.
* ``POST /platform/profile/delete`` — Art. 17: password re-confirmation,
  refused while the Identity is the last active administrator of an
  active tenant (the tenant would be left without anyone who can manage
  it). Erases the Identity and every tenant-side User / Contact it logs
  in as, deactivates the memberships, writes the erasure into each
  tenant's audit log (without the erased name or e-mail) and clears the
  platform session.
"""

from __future__ import annotations

from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.i18n import t as _t
from app.logging import get_logger
from app.models.customer import CustomerContact
from app.models.enums import UserRole
from app.models.tenant import Tenant
from app.models.user import User
from app.platform.deps import get_platform_db, require_identity
from app.platform.models import MEMBERSHIP_ACCESS_SUPPORT, Identity, TenantMembership
from app.platform.session import clear_platform_session
from app.security.csrf import verify_csrf
from app.security.rate_limit import limit as rate_limit

log = get_logger("app.platform.gdpr")

router = APIRouter(tags=["platform-gdpr"], dependencies=[Depends(verify_csrf)])


def _redirect_profile(request: Request, *, error: str | None = None) -> RedirectResponse:
    url = "/platform/profile"
    if error:
        url += "?error=" + quote(error)
    return RedirectResponse(url=url, status_code=303)


async def _memberships(db: AsyncSession, identity: Identity) -> list[TenantMembership]:
    return list(
        (
            await db.execute(
                select(TenantMembership).where(TenantMembership.identity_id == identity.id)
            )
        )
        .scalars()
        .all()
    )


async def tenants_where_last_admin(db: AsyncSession, identity: Identity) -> list[Tenant]:
    """Active tenants in which this identity is the only active admin.

    Support-access grants don't count either way: a platform admin's
    support user is not the tenant's administrator.
    """
    blocked: list[Tenant] = []
    for m in await _memberships(db, identity):
        if m.user_id is None or m.access_type == MEMBERSHIP_ACCESS_SUPPORT or not m.is_active:
            continue
        user = (await db.execute(select(User).where(User.id == m.user_id))).scalar_one_or_none()
        if user is None or not user.is_active or user.role != UserRole.TENANT_ADMIN:
            continue
        tenant = (await db.execute(select(Tenant).where(Tenant.id == m.tenant_id))).scalar_one()
        if not tenant.is_active:
            continue
        support_user_ids = select(TenantMembership.user_id).where(
            TenantMembership.tenant_id == tenant.id,
            TenantMembership.access_type == MEMBERSHIP_ACCESS_SUPPORT,
            TenantMembership.user_id.is_not(None),
        )
        other_admin = (
            await db.execute(
                select(User.id)
                .where(
                    User.tenant_id == tenant.id,
                    User.role == UserRole.TENANT_ADMIN,
                    User.is_active.is_(True),
                    User.id != user.id,
                    User.id.not_in(support_user_ids),
                )
                .limit(1)
            )
        ).first()
        if other_admin is None:
            blocked.append(tenant)
    return blocked


@router.get("/platform/profile", response_class=HTMLResponse)
async def identity_profile(
    request: Request,
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_platform_db),
) -> HTMLResponse:
    blocked = await tenants_where_last_admin(db, identity)
    html = request.app.state.templates.render(
        request,
        "platform/profile.html",
        {
            "identity": identity,
            "principal": None,
            "blocked_tenants": blocked,
            "error": request.query_params.get("error"),
            "notice": request.query_params.get("notice"),
        },
    )
    return HTMLResponse(html)


@router.get("/platform/profile/export")
async def identity_export(
    request: Request,
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_platform_db),
) -> Response:
    from app.services.gdpr_service import export_for_identity

    payload = await export_for_identity(db, identity=identity)
    log.info("platform.identity_exported", identity_id=str(identity.id))
    filename = f"assoluto-account-export-{date.today().isoformat()}.json"
    return JSONResponse(
        payload,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/platform/profile/delete")
@rate_limit("5/15 minutes")
async def identity_delete(
    request: Request,
    password: str = Form(...),
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_platform_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    from app.security.passwords import verify_password
    from app.services.audit_service import ActorInfo
    from app.services.audit_service import record as audit_record
    from app.services.gdpr_service import erase_contact, erase_identity, erase_user

    if not identity.password_hash or not verify_password(password, identity.password_hash):
        return _redirect_profile(
            request, error=_t(request, "Password does not match — deletion cancelled.")
        )

    blocked = await tenants_where_last_admin(db, identity)
    if blocked:
        return _redirect_profile(
            request,
            error=_t(
                request,
                "You're the last administrator of a portal — promote someone else first.",
            ),
        )

    identity_id = identity.id
    erased_count = 0
    for m in await _memberships(db, identity):
        if m.user_id is not None:
            user = (await db.execute(select(User).where(User.id == m.user_id))).scalar_one_or_none()
            if user is not None and not user.email.endswith("@erased.invalid"):
                await erase_user(db, user=user)
                await audit_record(
                    db,
                    action="user.gdpr_erased",
                    entity_type="user",
                    entity_id=user.id,
                    entity_label=f"user {user.id}",
                    actor=ActorInfo(type="user", id=user.id, label="(erased user)"),
                    tenant_id=user.tenant_id,
                )
                erased_count += 1
        elif m.contact_id is not None:
            contact = (
                await db.execute(select(CustomerContact).where(CustomerContact.id == m.contact_id))
            ).scalar_one_or_none()
            if contact is not None and not contact.email.endswith("@erased.invalid"):
                await erase_contact(db, contact=contact)
                await audit_record(
                    db,
                    action="contact.gdpr_erased",
                    entity_type="contact",
                    entity_id=contact.id,
                    entity_label=f"contact {contact.id}",
                    actor=ActorInfo(type="contact", id=contact.id, label="(erased contact)"),
                    tenant_id=contact.tenant_id,
                )
                erased_count += 1
        m.is_active = False

    await erase_identity(db, identity=identity)
    await db.commit()
    log.info(
        "platform.identity_erased",
        identity_id=str(identity_id),
        tenant_records_erased=erased_count,
    )

    response = RedirectResponse(url="/platform/login?notice=account_deleted", status_code=303)
    clear_platform_session(response, domain=settings.platform_cookie_domain or None)
    return response
