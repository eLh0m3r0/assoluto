"""Tenant staff routes for managing customers and their contacts."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.deps import Principal, get_db, require_tenant_staff
from app.models.tenant import Tenant
from app.security.csrf import verify_csrf
from app.services.auth_service import (
    InvalidInvitation,
    create_invitation_token,
    invite_customer_contact,
)
from app.services.customer_service import (
    create_customer,
    get_customer,
    list_contacts_for_customer,
    list_customers,
    update_customer,
)
from app.tasks.email_tasks import send_invitation

router = APIRouter(prefix="/app", tags=["customers"], dependencies=[Depends(verify_csrf)])


def _templates(request: Request):
    return request.app.state.templates


def _tenant(principal: Principal, request: Request) -> Tenant:
    tenant = getattr(request.state, "tenant", None)
    if tenant is None:
        raise HTTPException(status_code=500, detail="Tenant not resolved")
    return tenant


@router.get("/customers", response_class=HTMLResponse)
async def customers_index(
    request: Request,
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    customers = await list_customers(db)
    html = _templates(request).render(
        request,
        "customers/list.html",
        {
            "principal": principal,
            "tenant": _tenant(principal, request),
            "customers": customers,
        },
    )
    return HTMLResponse(html)


@router.get("/customers/new", response_class=HTMLResponse)
async def customers_new_form(
    request: Request,
    principal: Principal = Depends(require_tenant_staff),
) -> HTMLResponse:
    html = _templates(request).render(
        request,
        "customers/form.html",
        {
            "principal": principal,
            "tenant": _tenant(principal, request),
            "form": {},
            "error": None,
            "notice": None,
        },
    )
    return HTMLResponse(html)


@router.post("/customers", response_class=HTMLResponse)
async def customers_create(
    request: Request,
    name: str = Form(...),
    ico: str = Form(""),
    dic: str = Form(""),
    notes: str = Form(""),
    can_add_items: str = Form("on"),
    can_use_catalog: str = Form(""),
    can_set_prices: str = Form(""),
    can_upload_files: str = Form("on"),
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> Response:
    order_perms = {
        "can_add_items": can_add_items == "on",
        "can_use_catalog": can_use_catalog == "on",
        "can_set_prices": can_set_prices == "on",
        "can_upload_files": can_upload_files == "on",
    }
    try:
        customer = await create_customer(
            db,
            tenant_id=principal.tenant_id,
            name=name,
            ico=ico,
            dic=dic,
            notes=notes,
            order_permissions=order_perms,
        )
    except ValueError as exc:
        html = _templates(request).render(
            request,
            "customers/form.html",
            {
                "principal": principal,
                "tenant": _tenant(principal, request),
                "form": {"name": name, "ico": ico, "dic": dic, "notes": notes},
                "error": str(exc),
                "notice": None,
            },
        )
        return HTMLResponse(html, status_code=400)

    return RedirectResponse(url=f"/app/customers/{customer.id}", status_code=303)


@router.get("/customers/{customer_id}", response_class=HTMLResponse)
async def customers_detail(
    customer_id: UUID,
    request: Request,
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    customer = await get_customer(db, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")

    contacts = await list_contacts_for_customer(db, customer_id)
    html = _templates(request).render(
        request,
        "customers/detail.html",
        {
            "principal": principal,
            "tenant": _tenant(principal, request),
            "customer": customer,
            "contacts": contacts,
            "error": None,
            "notice": None,
        },
    )
    return HTMLResponse(html)


@router.get("/customers/{customer_id}/edit", response_class=HTMLResponse)
async def customers_edit_form(
    customer_id: UUID,
    request: Request,
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    customer = await get_customer(db, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    perms = customer.order_permissions or {}
    html = _templates(request).render(
        request,
        "customers/form.html",
        {
            "principal": principal,
            "tenant": _tenant(principal, request),
            "customer": customer,
            "form": {
                "name": customer.name,
                "ico": customer.ico or "",
                "dic": customer.dic or "",
                "notes": customer.notes or "",
                "can_add_items": "on" if perms.get("can_add_items", True) else "",
                "can_use_catalog": "on" if perms.get("can_use_catalog", True) else "",
                "can_set_prices": "on" if perms.get("can_set_prices", False) else "",
                "can_upload_files": "on" if perms.get("can_upload_files", True) else "",
            },
            "error": None,
            "notice": None,
        },
    )
    return HTMLResponse(html)


@router.post("/customers/{customer_id}", response_class=HTMLResponse)
async def customers_update(
    customer_id: UUID,
    request: Request,
    name: str = Form(...),
    ico: str = Form(""),
    dic: str = Form(""),
    notes: str = Form(""),
    can_add_items: str = Form(""),
    can_use_catalog: str = Form(""),
    can_set_prices: str = Form(""),
    can_upload_files: str = Form(""),
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> Response:
    customer = await get_customer(db, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    order_perms = {
        "can_add_items": can_add_items == "on",
        "can_use_catalog": can_use_catalog == "on",
        "can_set_prices": can_set_prices == "on",
        "can_upload_files": can_upload_files == "on",
    }
    try:
        await update_customer(
            db,
            customer,
            name=name,
            ico=ico,
            dic=dic,
            notes=notes,
            order_permissions=order_perms,
        )
    except ValueError as exc:
        html = _templates(request).render(
            request,
            "customers/form.html",
            {
                "principal": principal,
                "tenant": _tenant(principal, request),
                "customer": customer,
                "form": {
                    "name": name,
                    "ico": ico,
                    "dic": dic,
                    "notes": notes,
                    "can_add_items": can_add_items,
                    "can_use_catalog": can_use_catalog,
                    "can_set_prices": can_set_prices,
                    "can_upload_files": can_upload_files,
                },
                "error": str(exc),
                "notice": None,
            },
        )
        return HTMLResponse(html, status_code=400)

    return RedirectResponse(url=f"/app/customers/{customer.id}", status_code=303)


@router.post("/customers/{customer_id}/contacts", response_class=HTMLResponse)
async def customers_invite_contact(
    customer_id: UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    email: str = Form(...),
    full_name: str = Form(...),
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    customer = await get_customer(db, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")

    try:
        contact = await invite_customer_contact(
            db,
            tenant_id=principal.tenant_id,
            customer_id=customer_id,
            email=email,
            full_name=full_name,
        )
    except (InvalidInvitation, IntegrityError) as exc:
        contacts = await list_contacts_for_customer(db, customer_id)
        html = _templates(request).render(
            request,
            "customers/detail.html",
            {
                "principal": principal,
                "tenant": _tenant(principal, request),
                "customer": customer,
                "contacts": contacts,
                "error": (
                    "Kontakt se stejným e-mailem už existuje."
                    if isinstance(exc, IntegrityError)
                    else str(exc)
                ),
                "notice": None,
            },
        )
        return HTMLResponse(html, status_code=400)

    # Generate signed token and enqueue the invitation email as a
    # background task so we don't block the request on SMTP I/O.
    # Explicit commit first — see CLAUDE.md "BackgroundTasks + explicit
    # commit" for why this is mandatory.
    await db.commit()
    token = create_invitation_token(
        settings.app_secret_key,
        tenant_id=principal.tenant_id,
        contact_id=contact.id,
    )
    sender = request.app.state.email_sender
    tenant = _tenant(principal, request)
    from app.urls import tenant_base_url

    invite_url = f"{tenant_base_url(settings, tenant)}/invite/accept?token={token}"
    background_tasks.add_task(
        send_invitation,
        sender,
        to=contact.email,
        tenant_name=tenant.name,
        customer_name=customer.name,
        contact_name=contact.full_name,
        invite_url=invite_url,
    )

    return RedirectResponse(url=f"/app/customers/{customer.id}", status_code=303)
