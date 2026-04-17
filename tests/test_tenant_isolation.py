"""Integration test: verify Postgres Row-Level Security isolates tenants.

Requires a running PostgreSQL at `DATABASE_URL` (application role) and
`DATABASE_OWNER_URL` (table owner role). Marked with `postgres`; skipped
automatically when the DB isn't reachable.

Strategy:
1. Open an OWNER connection — bypasses RLS — to wipe state and seed two
   tenants with a customer + contact each.
2. Open an APPLICATION connection (portal_app, non-owner) for each tenant,
   set `app.tenant_id` via `SET LOCAL`, and assert it sees only its own
   rows.
3. Verify that inserting into a tenant-owned table with the wrong
   `app.tenant_id` fails because of the policy's WITH CHECK clause.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.asset import Asset
from app.models.customer import Customer, CustomerContact
from app.models.enums import CustomerContactRole, OrderStatus, UserRole
from app.models.order import Order
from app.models.product import Product
from app.models.tenant import Tenant
from app.models.user import User

pytestmark = pytest.mark.postgres

APP_URL_ENV = "DATABASE_URL"
OWNER_URL_ENV = "DATABASE_OWNER_URL"
APP_URL_DEFAULT = "postgresql+asyncpg://portal_app:portal_app@localhost:5432/portal"
OWNER_URL_DEFAULT = "postgresql+asyncpg://portal:portal@localhost:5432/portal"


@pytest.fixture
async def app_engine():  # type: ignore[misc]
    eng = create_async_engine(os.environ.get(APP_URL_ENV, APP_URL_DEFAULT), future=True)
    yield eng
    await eng.dispose()


@pytest.fixture
async def owner_engine():  # type: ignore[misc]
    eng = create_async_engine(os.environ.get(OWNER_URL_ENV, OWNER_URL_DEFAULT), future=True)
    yield eng
    await eng.dispose()


@pytest.fixture
async def seeded_tenants(owner_engine):  # type: ignore[misc]
    """Wipe state, create two tenants with seed data, yield their IDs.

    Uses the owner engine so RLS is bypassed automatically (the table owner
    is not subject to policies). After the test, clean up with the same
    owner engine.
    """
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)

    async def wipe() -> None:
        async with owner_engine.begin() as conn:
            await conn.execute(text("DELETE FROM asset_movements"))
            await conn.execute(text("DELETE FROM assets"))
            await conn.execute(text("DELETE FROM order_items"))
            await conn.execute(text("DELETE FROM order_comments"))
            await conn.execute(text("DELETE FROM order_status_history"))
            await conn.execute(text("DELETE FROM orders"))
            await conn.execute(text("DELETE FROM products"))
            await conn.execute(text("DELETE FROM customer_contacts"))
            await conn.execute(text("DELETE FROM customers"))
            await conn.execute(text("DELETE FROM users"))
            await conn.execute(text("DELETE FROM tenants"))

    await wipe()

    tenants: dict[str, Tenant] = {}
    customers: dict[str, Customer] = {}
    orders: dict[str, Order] = {}
    products: dict[str, Product] = {}
    assets: dict[str, Asset] = {}

    async with sm() as session, session.begin():
        for slug in ("alpha", "beta"):
            tenant = Tenant(
                id=uuid4(),
                slug=slug,
                name=f"{slug} s.r.o.",
                billing_email=f"billing@{slug}.cz",
                storage_prefix=f"tenants/{slug}/",
            )
            session.add(tenant)
            await session.flush()

            user = User(
                id=uuid4(),
                tenant_id=tenant.id,
                email=f"owner@{slug}.cz",
                full_name=f"{slug} Owner",
                role=UserRole.TENANT_ADMIN,
                password_hash="fake-hash",
            )
            customer = Customer(
                id=uuid4(),
                tenant_id=tenant.id,
                name=f"{slug}-ACME",
                ico="12345678",
            )
            session.add_all([user, customer])
            await session.flush()

            contact = CustomerContact(
                id=uuid4(),
                tenant_id=tenant.id,
                customer_id=customer.id,
                email=f"jan@{slug}.cz",
                full_name="Jan Novák",
                role=CustomerContactRole.CUSTOMER_USER,
            )
            order = Order(
                id=uuid4(),
                tenant_id=tenant.id,
                customer_id=customer.id,
                number=f"2026-{slug}-001",
                title=f"{slug} test order",
                status=OrderStatus.DRAFT,
            )
            product = Product(
                id=uuid4(),
                tenant_id=tenant.id,
                sku=f"{slug}-SKU-001",
                name=f"{slug} Widget",
            )
            asset = Asset(
                id=uuid4(),
                tenant_id=tenant.id,
                customer_id=customer.id,
                code=f"{slug}-ASSET-001",
                name=f"{slug} Machine",
            )
            session.add_all([contact, order, product, asset])
            await session.flush()

            tenants[slug] = tenant
            customers[slug] = customer
            orders[slug] = order
            products[slug] = product
            assets[slug] = asset

    yield tenants, customers, orders, products, assets

    await wipe()


async def test_rls_isolates_two_tenants(app_engine, seeded_tenants) -> None:
    tenants, customers, orders, products, assets = seeded_tenants
    sm = async_sessionmaker(app_engine, expire_on_commit=False)

    # Tenant A sees only its own rows.
    async with sm() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": str(tenants["alpha"].id)},
        )

        rows = (await session.execute(select(Customer))).scalars().all()
        assert len(rows) == 1
        assert rows[0].id == customers["alpha"].id

        users = (await session.execute(select(User))).scalars().all()
        assert len(users) == 1
        assert users[0].email == "owner@alpha.cz"

        contacts = (await session.execute(select(CustomerContact))).scalars().all()
        assert len(contacts) == 1
        assert contacts[0].email == "jan@alpha.cz"

        order_rows = (await session.execute(select(Order))).scalars().all()
        assert len(order_rows) == 1
        assert order_rows[0].id == orders["alpha"].id

        product_rows = (await session.execute(select(Product))).scalars().all()
        assert len(product_rows) == 1
        assert product_rows[0].id == products["alpha"].id

        asset_rows = (await session.execute(select(Asset))).scalars().all()
        assert len(asset_rows) == 1
        assert asset_rows[0].id == assets["alpha"].id

    # Tenant B sees only its own rows.
    async with sm() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": str(tenants["beta"].id)},
        )
        rows = (await session.execute(select(Customer))).scalars().all()
        assert len(rows) == 1
        assert rows[0].id == customers["beta"].id

        order_rows = (await session.execute(select(Order))).scalars().all()
        assert len(order_rows) == 1
        assert order_rows[0].id == orders["beta"].id

        product_rows = (await session.execute(select(Product))).scalars().all()
        assert len(product_rows) == 1
        assert product_rows[0].sku == "beta-SKU-001"

        asset_rows = (await session.execute(select(Asset))).scalars().all()
        assert len(asset_rows) == 1
        assert asset_rows[0].code == "beta-ASSET-001"


async def test_session_without_tenant_id_sees_no_rows(app_engine, seeded_tenants) -> None:
    # Unpack (fixture now returns 5-tuple)
    """If `app.tenant_id` isn't set, the policy evaluates to NULL → no rows."""
    sm = async_sessionmaker(app_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        # Deliberately no SET LOCAL; the policy compares against
        # current_setting('app.tenant_id', true) which returns NULL.
        rows = (await session.execute(select(Customer))).scalars().all()
        assert rows == []


async def test_insert_with_wrong_tenant_id_is_rejected(app_engine, seeded_tenants) -> None:
    """Policy WITH CHECK blocks writes under a different tenant's context."""
    tenants, *_ = seeded_tenants
    sm = async_sessionmaker(app_engine, expire_on_commit=False)

    with pytest.raises(DBAPIError, match="row-level security"):
        async with sm() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"),
                {"t": str(tenants["alpha"].id)},
            )
            session.add(
                User(
                    id=uuid4(),
                    tenant_id=tenants["beta"].id,  # wrong tenant!
                    email="attacker@wrong.cz",
                    full_name="Attacker",
                    role=UserRole.TENANT_STAFF,
                )
            )
            await session.flush()


async def test_cross_tenant_order_write_rejected(app_engine, seeded_tenants) -> None:
    """RLS blocks inserting an order under the wrong tenant context."""
    tenants, customers, *_ = seeded_tenants
    sm = async_sessionmaker(app_engine, expire_on_commit=False)

    with pytest.raises(DBAPIError, match="row-level security"):
        async with sm() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"),
                {"t": str(tenants["alpha"].id)},
            )
            session.add(
                Order(
                    id=uuid4(),
                    tenant_id=tenants["beta"].id,
                    customer_id=customers["beta"].id,
                    number="2026-attack-001",
                    title="cross-tenant attack",
                    status=OrderStatus.DRAFT,
                )
            )
            await session.flush()


async def test_cross_tenant_product_write_rejected(app_engine, seeded_tenants) -> None:
    """RLS blocks inserting a product under the wrong tenant context."""
    tenants, *_ = seeded_tenants
    sm = async_sessionmaker(app_engine, expire_on_commit=False)

    with pytest.raises(DBAPIError, match="row-level security"):
        async with sm() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"),
                {"t": str(tenants["alpha"].id)},
            )
            session.add(
                Product(
                    id=uuid4(),
                    tenant_id=tenants["beta"].id,
                    sku="ATTACK-SKU",
                    name="Cross-tenant product",
                )
            )
            await session.flush()


async def test_cross_tenant_asset_write_rejected(app_engine, seeded_tenants) -> None:
    """RLS blocks inserting an asset under the wrong tenant context."""
    tenants, customers, *_ = seeded_tenants
    sm = async_sessionmaker(app_engine, expire_on_commit=False)

    with pytest.raises(DBAPIError, match="row-level security"):
        async with sm() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"),
                {"t": str(tenants["alpha"].id)},
            )
            session.add(
                Asset(
                    id=uuid4(),
                    tenant_id=tenants["beta"].id,
                    customer_id=customers["beta"].id,
                    code="ATTACK-ASSET",
                    name="Cross-tenant asset",
                )
            )
            await session.flush()
