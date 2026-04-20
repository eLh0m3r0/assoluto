"""Product catalog routes (tenant staff only)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import Principal, get_db, require_tenant_staff
from app.security.csrf import verify_csrf
from app.services.customer_service import list_customers
from app.services.product_service import (
    DuplicateProductSku,
    ProductError,
    create_product,
    deactivate_product,
    get_product,
    list_products,
    search_products,
    update_product,
)

router = APIRouter(prefix="/app", tags=["products"], dependencies=[Depends(verify_csrf)])


def _templates(request: Request):
    return request.app.state.templates


def _tenant(request: Request):
    tenant = getattr(request.state, "tenant", None)
    if tenant is None:
        raise HTTPException(status_code=500, detail="Tenant not resolved")
    return tenant


@router.get("/products", response_class=HTMLResponse)
async def products_index(
    request: Request,
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    products = await list_products(db)
    customers = await list_customers(db)
    customer_by_id = {c.id: c for c in customers}
    html = _templates(request).render(
        request,
        "products/list.html",
        {
            "principal": principal,
            "tenant": _tenant(request),
            "products": products,
            "customer_by_id": customer_by_id,
        },
    )
    return HTMLResponse(html)


@router.get("/products/new", response_class=HTMLResponse)
async def products_new_form(
    request: Request,
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    customers = await list_customers(db)
    html = _templates(request).render(
        request,
        "products/form.html",
        {
            "principal": principal,
            "tenant": _tenant(request),
            "customers": customers,
            "form": {},
            "error": None,
            "notice": None,
        },
    )
    return HTMLResponse(html)


@router.post("/products", response_class=HTMLResponse)
async def products_create(
    request: Request,
    sku: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    unit: str = Form("ks"),
    default_price: str = Form(""),
    customer_id: str = Form(""),
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> Response:
    price_dec: Decimal | None = None
    if default_price.strip():
        try:
            price_dec = Decimal(default_price)
        except (InvalidOperation, ValueError):
            raise HTTPException(status_code=400, detail="Invalid price") from None

    target_customer_id: UUID | None = None
    if customer_id.strip():
        try:
            target_customer_id = UUID(customer_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid customer_id") from None

    try:
        await create_product(
            db,
            tenant_id=principal.tenant_id,
            sku=sku,
            name=name,
            description=description or None,
            unit=unit,
            default_price=price_dec,
            customer_id=target_customer_id,
        )
    except (ProductError, IntegrityError) as exc:
        from app.i18n import t as _t

        customers = await list_customers(db)
        if isinstance(exc, (DuplicateProductSku, IntegrityError)):
            error = _t(request, "A product with this SKU already exists.")
        else:
            error = str(exc)
        html = _templates(request).render(
            request,
            "products/form.html",
            {
                "principal": principal,
                "tenant": _tenant(request),
                "customers": customers,
                "form": {
                    "sku": sku,
                    "name": name,
                    "description": description,
                    "unit": unit,
                    "default_price": default_price,
                    "customer_id": customer_id,
                },
                "error": error,
                "notice": None,
            },
        )
        return HTMLResponse(html, status_code=400)

    return RedirectResponse(url="/app/products", status_code=303)


@router.get("/products/search")
async def products_search(
    q: str = "",
    customer_id: str = "",
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> Response:
    # Defined BEFORE ``/products/{product_id}`` because Starlette's route
    # matcher tries routes in registration order — a literal path must
    # win against a path-param route that would otherwise swallow
    # ``search`` and hit the 422 UUID validator.
    if len(q.strip()) < 1:
        return JSONResponse({"results": []})
    target: UUID | None = None
    if customer_id:
        try:
            target = UUID(customer_id)
        except ValueError:
            target = None

    results = await search_products(db, query=q, customer_id=target, limit=20)
    return JSONResponse(
        {
            "results": [
                {
                    "id": str(p.id),
                    "sku": p.sku,
                    "name": p.name,
                    "unit": p.unit,
                    "default_price": (
                        float(p.default_price) if p.default_price is not None else None
                    ),
                    "currency": p.currency,
                }
                for p in results
            ]
        }
    )


@router.get("/products/{product_id}", response_class=HTMLResponse)
async def products_detail(
    product_id: UUID,
    request: Request,
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    product = await get_product(db, product_id)
    if product is None or not product.is_active:
        raise HTTPException(status_code=404, detail="Product not found")
    customers = await list_customers(db)
    customer_by_id = {c.id: c for c in customers}
    html = _templates(request).render(
        request,
        "products/detail.html",
        {
            "principal": principal,
            "tenant": _tenant(request),
            "product": product,
            "customer_by_id": customer_by_id,
        },
    )
    return HTMLResponse(html)


@router.get("/products/{product_id}/edit", response_class=HTMLResponse)
async def products_edit_form(
    product_id: UUID,
    request: Request,
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    product = await get_product(db, product_id)
    if product is None or not product.is_active:
        raise HTTPException(status_code=404, detail="Product not found")
    customers = await list_customers(db)
    html = _templates(request).render(
        request,
        "products/form.html",
        {
            "principal": principal,
            "tenant": _tenant(request),
            "customers": customers,
            "product": product,
            "form": {
                "sku": product.sku,
                "name": product.name,
                "description": product.description or "",
                "unit": product.unit,
                "default_price": (
                    str(product.default_price) if product.default_price is not None else ""
                ),
                "customer_id": str(product.customer_id) if product.customer_id else "",
            },
            "error": None,
            "notice": None,
        },
    )
    return HTMLResponse(html)


@router.post("/products/{product_id}", response_class=HTMLResponse)
async def products_update(
    product_id: UUID,
    request: Request,
    sku: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    unit: str = Form("ks"),
    default_price: str = Form(""),
    customer_id: str = Form(""),
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> Response:
    product = await get_product(db, product_id)
    if product is None or not product.is_active:
        raise HTTPException(status_code=404, detail="Product not found")

    price_dec: Decimal | None = None
    if default_price.strip():
        try:
            price_dec = Decimal(default_price)
        except (InvalidOperation, ValueError):
            raise HTTPException(status_code=400, detail="Invalid price") from None

    target_customer_id: UUID | None = None
    if customer_id.strip():
        try:
            target_customer_id = UUID(customer_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid customer_id") from None

    try:
        await update_product(
            db,
            product,
            sku=sku,
            name=name,
            description=description or None,
            unit=unit,
            default_price=price_dec,
            customer_id=target_customer_id,
        )
    except (ProductError, IntegrityError) as exc:
        from app.i18n import t as _t

        customers = await list_customers(db)
        error = (
            _t(request, "A product with this SKU already exists.")
            if isinstance(exc, (DuplicateProductSku, IntegrityError))
            else str(exc)
        )
        html = _templates(request).render(
            request,
            "products/form.html",
            {
                "principal": principal,
                "tenant": _tenant(request),
                "customers": customers,
                "product": product,
                "form": {
                    "sku": sku,
                    "name": name,
                    "description": description,
                    "unit": unit,
                    "default_price": default_price,
                    "customer_id": customer_id,
                },
                "error": error,
                "notice": None,
            },
        )
        return HTMLResponse(html, status_code=400)

    return RedirectResponse(url=f"/app/products/{product.id}", status_code=303)


@router.post("/products/{product_id}/delete", response_class=HTMLResponse)
async def products_delete(
    product_id: UUID,
    request: Request,
    principal: Principal = Depends(require_tenant_staff),
    db: AsyncSession = Depends(get_db),
) -> Response:
    product = await get_product(db, product_id)
    if product is None or not product.is_active:
        raise HTTPException(status_code=404, detail="Product not found")
    await deactivate_product(db, product)
    return RedirectResponse(url="/app/products", status_code=303)
