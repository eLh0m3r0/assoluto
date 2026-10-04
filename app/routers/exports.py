"""Accounting exports: orders → POHODA / Money S3 XML (tenant admins only).

GET routes:

* ``/app/admin/exports`` — small options page (statuses, period,
  customer, VAT rate, the tenant's own IČO).
* ``/app/admin/exports/pohoda.xml`` — POHODA download. Also linked from
  the staff order list with the list's current filters, so "what I see
  is what I export".
* ``/app/admin/exports/money-s3.xml`` — the same selection for Money S3.

All are read-only GETs, so CSRF (POST-only double-submit) does not
apply; the router still carries ``verify_csrf`` like every other router.
Tenant isolation comes from the RLS-scoped session in ``get_db``.
"""

from __future__ import annotations

from datetime import date, datetime
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import Principal, get_db, require_tenant_staff
from app.i18n import t as _t
from app.models.enums import STATUS_LABELS, OrderStatus, UserRole
from app.security.csrf import verify_csrf
from app.services.accounting_export import (
    DEFAULT_STATUSES,
    DEFAULT_VAT_RATE,
    MAX_EXPORT_ORDERS,
    MONEY_S3_MEDIA_TYPE,
    POHODA_MEDIA_TYPE,
    VAT_RATES,
    ExportFilters,
    NothingToExport,
    TooManyOrders,
    build_money_s3_xml,
    build_pohoda_datapack,
    load_orders_for_export,
    parse_statuses,
)
from app.services.customer_service import list_customers
from app.services.order_service import UNASSIGNED, ActorRef

router = APIRouter(
    prefix="/app/admin/exports", tags=["exports"], dependencies=[Depends(verify_csrf)]
)

EXPORTS_PAGE = "/app/admin/exports"

# Query params the options page round-trips (and the error redirect
# preserves so the form comes back pre-filled).
_FORWARDED_PARAMS = ("status", "customer", "from", "to", "q", "assigned", "vat_rate", "ico")


def _require_tenant_admin(principal: Principal) -> None:
    """Exports hand out the whole order book — admin only.

    ``require_tenant_staff`` has already turned contacts away with 403.
    """
    if principal.role != UserRole.TENANT_ADMIN.value:
        raise HTTPException(status_code=403, detail="Tenant admin required")


def _tenant(request: Request):
    tenant = getattr(request.state, "tenant", None)
    if tenant is None:
        raise HTTPException(status_code=500, detail="Tenant not resolved")
    return tenant


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _parse_uuid(raw: str | None) -> UUID | None:
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


def _parse_assigned(raw: str | None, principal: Principal) -> UUID | str | None:
    # Same semantics as the order list's ``assigned`` filter.
    if not raw:
        return None
    if raw == "me":
        return principal.id
    if raw == UNASSIGNED:
        return UNASSIGNED
    return _parse_uuid(raw)


def _tenant_ico(tenant) -> str:
    """The tenant's own IČO, if billing details were filled in."""
    blob = getattr(tenant, "settings", None) or {}
    return str(blob.get("billing_ico") or "").strip()


def _filters_from_request(request: Request, principal: Principal) -> ExportFilters:
    params = request.query_params
    return ExportFilters(
        statuses=parse_statuses(params.getlist("status")),
        customer_id=_parse_uuid(params.get("customer")),
        date_from=_parse_date(params.get("from")),
        date_to=_parse_date(params.get("to")),
        q=(params.get("q") or "").strip() or None,
        assigned_to=_parse_assigned(params.get("assigned"), principal),
    )


def _back_to_page(request: Request, error: str) -> RedirectResponse:
    kept = [(k, v) for k, v in request.query_params.multi_items() if k in _FORWARDED_PARAMS]
    kept.append(("error", error))
    return RedirectResponse(url=f"{EXPORTS_PAGE}?{urlencode(kept)}", status_code=303)


@router.get("", response_class=HTMLResponse)
async def exports_page(
    request: Request,
    error: str | None = None,
    notice: str | None = None,
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    _require_tenant_admin(principal)
    tenant = _tenant(request)
    params = request.query_params
    raw_statuses = params.getlist("status")
    selected = parse_statuses(raw_statuses)
    vat_rate = params.get("vat_rate") or DEFAULT_VAT_RATE
    html = request.app.state.templates.render(
        request,
        "admin/exports.html",
        {
            "principal": principal,
            "tenant": tenant,
            "error": error,
            "notice": notice,
            "customers": await list_customers(db),
            "status_options": [(s.value, STATUS_LABELS[s]) for s in OrderStatus],
            "selected_statuses": {s.value for s in selected},
            "default_statuses": {s.value for s in DEFAULT_STATUSES},
            "filters": {
                "customer": params.get("customer") or "",
                "from": params.get("from") or "",
                "to": params.get("to") or "",
                "q": params.get("q") or "",
                "assigned": params.get("assigned") or "",
            },
            "vat_rate": vat_rate if vat_rate in VAT_RATES else DEFAULT_VAT_RATE,
            "ico": params.get("ico", _tenant_ico(tenant)),
            "max_orders": MAX_EXPORT_ORDERS,
        },
    )
    return HTMLResponse(html)


async def _export(
    request: Request,
    *,
    target: str,
    vat_rate: str,
    ico: str | None,
    principal: Principal,
    db: AsyncSession,
) -> Response:
    """Shared body of the POHODA / Money S3 downloads."""
    _require_tenant_admin(principal)
    tenant = _tenant(request)

    if vat_rate not in VAT_RATES:
        return _back_to_page(request, _t(request, "Unknown VAT rate."))

    filters = _filters_from_request(request, principal)
    actor = ActorRef(type=principal.type, id=principal.id, customer_id=principal.customer_id)
    try:
        orders = await load_orders_for_export(db, actor=actor, filters=filters)
    except TooManyOrders:
        return _back_to_page(
            request,
            _t(request, "Too many orders for one file. Narrow the period and export in parts."),
        )
    if not orders:
        return _back_to_page(request, _t(request, "No orders match the selected filters."))

    now = datetime.now()
    # ``ico`` absent → tenant's billing IČO; present but blank → omit on
    # purpose (the program then imports into the open accounting unit).
    target_ico = (_tenant_ico(tenant) if ico is None else ico.strip()) or None
    label = f"Assoluto {tenant.slug} {now:%Y-%m-%d %H:%M}"
    try:
        if target == "money":
            body = build_money_s3_xml(orders, ico=target_ico, vat_rate=vat_rate, description=label)
            media_type = MONEY_S3_MEDIA_TYPE
            filename = f"money-s3-objednavky-{now:%Y%m%d}.xml"
        else:
            body = build_pohoda_datapack(
                orders,
                pack_id=f"assoluto-{tenant.slug}-{now:%Y%m%d%H%M%S}",
                ico=target_ico,
                vat_rate=vat_rate,
                note=label,
            )
            media_type = POHODA_MEDIA_TYPE
            filename = f"pohoda-objednavky-{now:%Y%m%d}.xml"
    except NothingToExport:  # pragma: no cover - guarded above
        return _back_to_page(request, _t(request, "No orders match the selected filters."))

    return Response(
        content=body,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/pohoda.xml")
async def export_pohoda_xml(
    request: Request,
    vat_rate: str = Query(DEFAULT_VAT_RATE),
    ico: str | None = Query(None),
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Download matching orders as a POHODA ``dataPack`` (received orders)."""
    return await _export(
        request, target="pohoda", vat_rate=vat_rate, ico=ico, principal=principal, db=db
    )


@router.get("/money-s3.xml")
async def export_money_s3_xml(
    request: Request,
    vat_rate: str = Query(DEFAULT_VAT_RATE),
    ico: str | None = Query(None),
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Download matching orders as Money S3 ``SeznamObjPrij`` (received orders)."""
    return await _export(
        request, target="money", vat_rate=vat_rate, ico=ico, principal=principal, db=db
    )
