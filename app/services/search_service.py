"""Global search service powering the ⌘K command palette.

Aggregates up to a few results per category (orders, customers, products)
for a single substring query and returns lightweight dicts the palette
fragment template can render without further DB lookups.

Authorization is applied inside this module — callers only need to pass
the current principal and a tenant-scoped DB session (RLS already
enforces cross-tenant isolation; this layer adds the per-customer scope
for contact principals).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import Principal
from app.models.customer import Customer
from app.models.order import Order
from app.services.product_service import search_products


def _ilike_pattern(q: str) -> str:
    """Escape basic LIKE wildcards then wrap in %…% for a substring match."""
    # Users typing literal `%` or `_` should not get surprise wildcards.
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def global_search(
    db: AsyncSession,
    *,
    principal: Principal,
    q: str,
    limit_per_section: int = 5,
) -> dict[str, list[dict[str, Any]]]:
    """Return up to `limit_per_section` hits per category.

    Shape:
        {
            "orders":    [{type, id, label, sublabel, href}, ...],
            "customers": [...],   # empty list for contacts
            "products":  [...],
        }

    Staff can search across every order and customer in the tenant.
    Contacts only ever see orders belonging to their own customer and a
    product catalog scoped via `search_products(customer_id=...)`; the
    customers section is always empty for them.
    """
    q = (q or "").strip()
    empty: dict[str, list[dict[str, Any]]] = {
        "orders": [],
        "customers": [],
        "products": [],
    }
    if len(q) < 2:
        return empty

    pattern = _ilike_pattern(q)
    is_staff = principal.is_staff
    contact_customer_id: UUID | None = (
        principal.customer_id if principal.type == "contact" else None
    )

    # ---------------------------------------------------------- orders
    # Staff can match on order.number, title, OR the customer's name
    # (joined). Contacts are strictly scoped to their own customer_id
    # — RLS already filters to their tenant, this filter narrows them
    # to their own customer's orders.
    order_stmt = (
        select(Order, Customer.name.label("customer_name"))
        .join(Customer, Customer.id == Order.customer_id)
        .order_by(Order.created_at.desc())
        .limit(limit_per_section)
    )
    if is_staff:
        order_stmt = order_stmt.where(
            or_(
                Order.number.ilike(pattern),
                Order.title.ilike(pattern),
                Customer.name.ilike(pattern),
            )
        )
    else:
        # Contacts: belt-and-braces — the principal's customer_id must
        # be non-NULL (if it were we wouldn't reach here in practice)
        # and we skip any customer_name matching entirely since that
        # would let a contact confirm the existence of other customers
        # by typing their name.
        if contact_customer_id is None:
            order_rows: list[Any] = []
        else:
            order_stmt = order_stmt.where(
                Order.customer_id == contact_customer_id,
                or_(
                    Order.number.ilike(pattern),
                    Order.title.ilike(pattern),
                ),
            )
    if is_staff or contact_customer_id is not None:
        order_rows = list((await db.execute(order_stmt)).all())
    else:
        order_rows = []

    orders_out = [
        {
            "type": "order",
            "id": str(row.Order.id),
            "label": row.Order.number,
            "sublabel": f"{row.customer_name} — {row.Order.title}" if is_staff else row.Order.title,
            "href": f"/app/orders/{row.Order.id}",
        }
        for row in order_rows
    ]

    # ---------------------------------------------------------- customers
    customers_out: list[dict[str, Any]] = []
    if is_staff:
        cust_stmt = (
            select(Customer)
            .where(
                Customer.is_active.is_(True),
                or_(
                    Customer.name.ilike(pattern),
                    Customer.ico.ilike(pattern),
                ),
            )
            .order_by(Customer.name)
            .limit(limit_per_section)
        )
        cust_rows = (await db.execute(cust_stmt)).scalars().all()
        customers_out = [
            {
                "type": "customer",
                "id": str(c.id),
                "label": c.name,
                "sublabel": c.ico or "",
                "href": f"/app/customers/{c.id}",
            }
            for c in cust_rows
        ]
    # Contacts intentionally get no customers section.

    # ---------------------------------------------------------- products
    # `search_products` already treats `customer_id=None` as "catalog-wide".
    # For contacts we scope via their customer so they only see shared
    # products + those dedicated to their company.
    products = await search_products(
        db,
        query=q,
        customer_id=contact_customer_id if not is_staff else None,
        limit=limit_per_section,
    )
    products_out = [
        {
            "type": "product",
            "id": str(p.id),
            "label": p.sku,
            "sublabel": p.name,
            "href": f"/app/products/{p.id}",
        }
        for p in products
    ]

    return {
        "orders": orders_out,
        "customers": customers_out,
        "products": products_out,
    }
