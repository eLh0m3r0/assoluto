"""Enter the public demo without signing up (CEO decision E3).

``GET /demo`` on the demo tenant's host shows two buttons; each one POSTs
(CSRF-protected, rate-limited per IP) to ``/demo/enter`` and gets an
ordinary session for one of the two seeded personas:

* ``staff``    — the shop's admin, ``vedouci@dilna-vzorova.example.com``;
* ``customer`` — the admin contact of its first client,
  ``nakup@ukazkova.example.com``.

No password is involved; what such a session may do is limited by
:mod:`app.demo.guard`. On any other tenant both routes are a plain 404.
"""

from __future__ import annotations

from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.demo.guard import is_public_demo_request
from app.demo.landing import flagship_quote
from app.demo.seed import DEMO_CONTACT_EMAIL, DEMO_STAFF_EMAIL
from app.deps import Principal, get_current_principal, get_current_tenant, get_db
from app.i18n import t as _t
from app.logging import get_logger
from app.models.customer import Customer, CustomerContact
from app.models.tenant import Tenant
from app.models.user import User
from app.security.csrf import verify_csrf
from app.security.rate_limit import _client_ip
from app.security.rate_limit import limit as rate_limit
from app.security.session import (
    SESSION_COOKIE_NAME,
    SessionData,
    clear_session,
    write_session,
)

log = get_logger("app.demo")

router = APIRouter(tags=["demo"], dependencies=[Depends(verify_csrf)])

#: Entries per IP. Generous enough to switch roles a few times, tight
#: enough that a script cannot mint sessions in bulk.
ENTER_RATE_LIMIT = "20/15 minutes"


def _require_public_demo(request: Request) -> None:
    if not is_public_demo_request(request):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


@router.get("/demo", response_class=HTMLResponse)
async def demo_index(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
    principal: Principal | None = Depends(get_current_principal),
    settings: Settings = Depends(get_settings),
) -> Response:
    # Unlike /auth/login this page does not bounce a signed-in visitor to
    # /app: switching between the two personas is the point. It offers a
    # "continue" link instead (CLAUDE.md §8 spirit).
    _require_public_demo(request)
    html = request.app.state.templates.render(
        request,
        "demo/enter.html",
        {
            "tenant": tenant,
            "principal": None,
            "signed_in_as": principal.type if principal else None,
            "error": request.query_params.get("error"),
        },
    )
    response = HTMLResponse(html)
    # A cookie for another tenant, or for a persona the nightly reset has
    # re-created, is dead weight: drop it (CLAUDE.md §13).
    if principal is None and request.cookies.get(SESSION_COOKIE_NAME):
        clear_session(response)
    return response


@router.post("/demo/enter")
@rate_limit(ENTER_RATE_LIMIT)
async def demo_enter(
    request: Request,
    role: str = Form(...),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    _require_public_demo(request)
    session_data: SessionData | None = None
    if role == "staff":
        user = (
            await db.execute(
                select(User).where(User.email == DEMO_STAFF_EMAIL, User.is_active.is_(True))
            )
        ).scalar_one_or_none()
        if user is not None:
            session_data = SessionData(
                principal_type="user",
                principal_id=str(user.id),
                tenant_id=str(tenant.id),
                session_version=user.session_version,
            )
    elif role == "customer":
        contact = (
            await db.execute(
                select(CustomerContact)
                .join(Customer, Customer.id == CustomerContact.customer_id)
                .where(
                    CustomerContact.email == DEMO_CONTACT_EMAIL,
                    CustomerContact.is_active.is_(True),
                    Customer.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if contact is not None:
            session_data = SessionData(
                principal_type="contact",
                principal_id=str(contact.id),
                tenant_id=str(tenant.id),
                customer_id=str(contact.customer_id),
                session_version=contact.session_version,
            )
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown role")

    if session_data is None:
        # Mid-reset, or the seed was changed by hand.
        log.warning("demo.enter.persona_missing", role=role)
        message = quote(_t(request, "The demo is being refreshed. Please try again in a minute."))
        return RedirectResponse(url=f"/demo?error={message}", status_code=303)

    # The customer persona lands on its open quote (P2-14): drawing,
    # price and the "Confirm" button are the whole point of the client
    # view. Falls back to the dashboard when no quote is waiting.
    target = "/app"
    if session_data.principal_type == "contact" and session_data.customer_id:
        flagship = await flagship_quote(db, customer_id=UUID(session_data.customer_id))
        if flagship is not None:
            target = f"/app/orders/{flagship.id}"

    log.info("demo.enter", role=role, ip=_client_ip(request))
    response = RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)
    write_session(response, settings.app_secret_key, session_data, secure=settings.is_production)
    return response
