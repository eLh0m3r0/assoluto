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

from app.models.customer import Customer, CustomerContact
from app.models.enums import CustomerContactRole, UserRole
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
            await conn.execute(text("DELETE FROM customer_contacts"))
            await conn.execute(text("DELETE FROM customers"))
            await conn.execute(text("DELETE FROM users"))
            await conn.execute(text("DELETE FROM tenants"))

    await wipe()

    tenants: dict[str, Tenant] = {}
    customers: dict[str, Customer] = {}

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
            session.add(contact)
            await session.flush()

            tenants[slug] = tenant
            customers[slug] = customer

    yield tenants, customers

    await wipe()


async def test_rls_isolates_two_tenants(app_engine, seeded_tenants) -> None:
    tenants, customers = seeded_tenants
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

    # Tenant B sees only its own rows.
    async with sm() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": str(tenants["beta"].id)},
        )
        rows = (await session.execute(select(Customer))).scalars().all()
        assert len(rows) == 1
        assert rows[0].id == customers["beta"].id


async def test_session_without_tenant_id_sees_no_rows(app_engine, seeded_tenants) -> None:
    """If `app.tenant_id` isn't set, the policy evaluates to NULL → no rows."""
    sm = async_sessionmaker(app_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        # Deliberately no SET LOCAL; the policy compares against
        # current_setting('app.tenant_id', true) which returns NULL.
        rows = (await session.execute(select(Customer))).scalars().all()
        assert rows == []


async def test_insert_with_wrong_tenant_id_is_rejected(app_engine, seeded_tenants) -> None:
    """Policy WITH CHECK blocks writes under a different tenant's context."""
    tenants, _ = seeded_tenants
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
