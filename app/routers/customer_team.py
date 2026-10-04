"""Customer-admin self-service: invite colleagues from the same company (IDEA-9).

A contact with role ``customer_admin`` manages who on *their* side has
portal access, so the supplier's staff no longer field "please add my
colleague" requests. Scope is deliberately narrow:

* only colleagues of the admin's own customer (``principal.customer_id``);
* new colleagues always get the plain ``customer_user`` role — promoting
  somebody to admin stays a supplier decision;
* plan limits apply exactly as for staff invites
  (``invite_customer_contact`` -> ``ensure_within_limit``);
* every invite is audited with the contact as actor, so the supplier
  sees it in the audit log;
* CSRF via the router dependency, rate-limited per client IP so the
  form can't be used as a mail cannon.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.deps import Principal, get_db, require_login
from app.i18n import t as _t
from app.models.enums import CustomerContactRole
from app.routers.customers import schedule_contact_invitation
from app.security.csrf import verify_csrf
from app.security.rate_limit import limit as rate_limit
from app.services import audit_service
from app.services.audit_service import actor_from_principal
from app.services.auth_service import InvalidInvitation, invite_customer_contact
from app.services.customer_service import get_customer, list_contacts_for_customer

router = APIRouter(prefix="/app/me", tags=["customer-team"], dependencies=[Depends(verify_csrf)])


async def require_customer_admin(
    principal: Principal = Depends(require_login),
) -> Principal:
    if principal.is_staff or principal.role != CustomerContactRole.CUSTOMER_ADMIN.value:
        raise HTTPException(status_code=403, detail="Client admin required")
    return principal


def _redirect(notice: str | None = None, error: str | None = None) -> RedirectResponse:
    if error:
        return RedirectResponse(url=f"/app/me/team?error={quote(error)}", status_code=303)
    return RedirectResponse(url=f"/app/me/team?notice={quote(notice or '')}", status_code=303)


@router.get("/team", response_class=HTMLResponse)
async def team_index(
    request: Request,
    notice: str | None = None,
    error: str | None = None,
    principal: Principal = Depends(require_customer_admin),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    customer = await get_customer(db, principal.customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    contacts = await list_contacts_for_customer(db, customer.id)
    html = request.app.state.templates.render(
        request,
        "me/team.html",
        {
            "principal": principal,
            "tenant": request.state.tenant,
            "customer": customer,
            "contacts": contacts,
            "notice": notice,
            "error": error,
        },
    )
    return HTMLResponse(html)


@router.post("/team/invite", response_class=HTMLResponse)
@rate_limit("20/hour")
async def team_invite(
    request: Request,
    background_tasks: BackgroundTasks,
    email: str = Form(...),
    full_name: str = Form(...),
    principal: Principal = Depends(require_customer_admin),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    customer = await get_customer(db, principal.customer_id)
    if customer is None or not customer.is_active:
        raise HTTPException(status_code=404, detail="Customer not found")
    if not (email or "").strip() or not (full_name or "").strip():
        return _redirect(error=_t(request, "Please fill in all fields."))

    try:
        async with db.begin_nested():
            contact = await invite_customer_contact(
                db,
                tenant_id=principal.tenant_id,
                customer_id=customer.id,
                email=email,
                full_name=full_name,
                role=CustomerContactRole.CUSTOMER_USER,
            )
    except IntegrityError:
        return _redirect(error=_t(request, "A contact with the same email already exists."))
    except InvalidInvitation:
        return _redirect(error=_t(request, "The invitation could not be created."))

    await audit_service.record(
        db,
        action="customer_contact.invited",
        entity_type="customer_contact",
        entity_id=contact.id,
        entity_label=f"{contact.full_name} <{contact.email}>",
        actor=actor_from_principal(principal),
        after={"customer": customer.name, "role": contact.role.value},
        tenant_id=principal.tenant_id,
    )
    # Commit before the background task reads anything (CLAUDE.md §2).
    await db.commit()
    schedule_contact_invitation(
        request,
        background_tasks,
        settings,
        tenant=request.state.tenant,
        customer=customer,
        contact=contact,
    )
    return _redirect(notice=_t(request, "Invitation sent."))
