"""Create a demo tenant + staff + customer + contact + sample data.

Handy for local development: run once after bringing up Postgres and
the portal will have everything you need to click through the flows.

Usage:
    python -m scripts.seed_dev
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models.asset import Asset, AssetMovement
from app.models.customer import Customer, CustomerContact
from app.models.enums import AssetMovementType, CustomerContactRole, OrderStatus, UserRole
from app.models.order import Order, OrderItem, OrderStatusHistory
from app.models.product import Product
from app.models.tenant import Tenant
from app.models.user import User
from app.security.passwords import hash_password


async def _run() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_owner_url, future=True)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    async with sm() as session, session.begin():
        tenant = Tenant(
            id=uuid4(),
            slug="4mex",
            name="4MEX s.r.o.",
            billing_email="billing@4mex.cz",
            storage_prefix="tenants/4mex/",
            next_order_seq=1,  # seed inserts order #2026-000001
        )
        session.add(tenant)
        await session.flush()

        owner = User(
            id=uuid4(),
            tenant_id=tenant.id,
            email="owner@4mex.cz",
            full_name="4MEX Owner",
            role=UserRole.TENANT_ADMIN,
            password_hash=hash_password("demo1234"),
        )
        acme = Customer(
            id=uuid4(),
            tenant_id=tenant.id,
            name="ACME s.r.o.",
            ico="12345678",
            dic="CZ12345678",
        )
        session.add_all([owner, acme])
        await session.flush()

        jan = CustomerContact(
            id=uuid4(),
            tenant_id=tenant.id,
            customer_id=acme.id,
            email="jan@acme.cz",
            full_name="Jan Novák",
            role=CustomerContactRole.CUSTOMER_ADMIN,
            password_hash=hash_password("demo1234"),
            invited_at=datetime.now(UTC),
            accepted_at=datetime.now(UTC),
        )
        session.add(jan)

        # Catalog: 2 shared, 1 ACME-specific.
        shared_a = Product(
            tenant_id=tenant.id,
            sku="SKU-100",
            name="Plech Al 2mm",
            unit="kg",
            default_price=Decimal("85.00"),
        )
        shared_b = Product(
            tenant_id=tenant.id,
            sku="SKU-101",
            name="Svařování TIG",
            unit="hod",
            default_price=Decimal("650.00"),
        )
        custom = Product(
            tenant_id=tenant.id,
            customer_id=acme.id,
            sku="ACME-001",
            name="Řezání na míru dle výkresu",
            unit="ks",
            default_price=Decimal("1200.00"),
        )
        session.add_all([shared_a, shared_b, custom])
        await session.flush()

        # A demo order in QUOTED state with two items.
        order = Order(
            id=uuid4(),
            tenant_id=tenant.id,
            customer_id=acme.id,
            number="2026-000001",
            title="Zakázka Q1 demo",
            status=OrderStatus.QUOTED,
            created_by_contact_id=jan.id,
            quoted_total=Decimal("2050.00"),
            currency="CZK",
            submitted_at=datetime.now(UTC),
        )
        session.add(order)
        await session.flush()

        session.add_all(
            [
                OrderItem(
                    tenant_id=tenant.id,
                    order_id=order.id,
                    product_id=custom.id,
                    position=0,
                    description="Řezání dle výkresu 01",
                    quantity=Decimal("1"),
                    unit="ks",
                    unit_price=Decimal("1200.00"),
                    line_total=Decimal("1200.00"),
                ),
                OrderItem(
                    tenant_id=tenant.id,
                    order_id=order.id,
                    product_id=shared_b.id,
                    position=1,
                    description="TIG svár",
                    quantity=Decimal("1.31"),
                    unit="hod",
                    unit_price=Decimal("650.00"),
                    line_total=Decimal("850.00"),
                ),
                OrderStatusHistory(
                    tenant_id=tenant.id,
                    order_id=order.id,
                    from_status=None,
                    to_status=OrderStatus.DRAFT,
                    changed_by_contact_id=jan.id,
                ),
                OrderStatusHistory(
                    tenant_id=tenant.id,
                    order_id=order.id,
                    from_status=OrderStatus.DRAFT,
                    to_status=OrderStatus.SUBMITTED,
                    changed_by_contact_id=jan.id,
                ),
                OrderStatusHistory(
                    tenant_id=tenant.id,
                    order_id=order.id,
                    from_status=OrderStatus.SUBMITTED,
                    to_status=OrderStatus.QUOTED,
                    changed_by_user_id=owner.id,
                ),
            ]
        )

        # A demo asset with a couple of movements.
        asset = Asset(
            tenant_id=tenant.id,
            customer_id=acme.id,
            code="AL-2MM",
            name="Plech Al 2mm (zásoba ACME)",
            unit="kg",
            current_quantity=Decimal("75"),
            location="Regál A3",
        )
        session.add(asset)
        await session.flush()
        session.add_all(
            [
                AssetMovement(
                    tenant_id=tenant.id,
                    asset_id=asset.id,
                    type=AssetMovementType.RECEIVE,
                    quantity=Decimal("100"),
                    note="Počáteční dodávka",
                    created_by_user_id=owner.id,
                ),
                AssetMovement(
                    tenant_id=tenant.id,
                    asset_id=asset.id,
                    type=AssetMovementType.CONSUME,
                    quantity=Decimal("-25"),
                    note="Spotřeba na zakázku 2026-000001",
                    reference_order_id=order.id,
                    created_by_user_id=owner.id,
                ),
            ]
        )

    await engine.dispose()
    print("Demo tenant seeded.")
    print("  URL:      http://4mex.localhost:8000/  (or set X-Tenant-Slug: 4mex)")
    print("  Staff:    owner@4mex.cz / demo1234")
    print("  Contact:  jan@acme.cz  / demo1234")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
