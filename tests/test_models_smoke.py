"""Smoke test: verify the ORM can create and query the Tenant model.

Uses an in-memory SQLite database for speed — no Postgres required for
basic model sanity. Tests that rely on Postgres-specific features (JSONB,
RLS, UUID columns with server defaults) live in integration tests that
only run when a real Postgres is available.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Import through the package so all model modules are registered.
from app import models  # noqa: F401
from app.db.base import Base
from app.models.customer import Customer, CustomerContact
from app.models.enums import CustomerContactRole, UserRole
from app.models.tenant import Tenant
from app.models.user import User


@pytest.fixture
async def sqlite_session() -> AsyncSession:  # type: ignore[misc]
    """Yield an in-memory SQLite async session with the schema created."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        yield session
    await engine.dispose()


async def test_create_and_query_tenant(sqlite_session: AsyncSession) -> None:
    tenant = Tenant(
        id=uuid4(),
        slug="4mex",
        name="4MEX s.r.o.",
        billing_email="billing@4mex.cz",
        storage_prefix="tenants/4mex/",
    )
    sqlite_session.add(tenant)
    await sqlite_session.commit()

    result = await sqlite_session.execute(select(Tenant).where(Tenant.slug == "4mex"))
    loaded = result.scalar_one()

    assert loaded.name == "4MEX s.r.o."
    assert loaded.billing_email == "billing@4mex.cz"
    assert loaded.next_order_seq == 0
    assert loaded.is_active is True
    assert loaded.settings == {}
    assert loaded.created_at is not None
    assert loaded.updated_at is not None


async def test_tenant_slug_is_unique(sqlite_session: AsyncSession) -> None:
    sqlite_session.add(
        Tenant(
            id=uuid4(),
            slug="same",
            name="A",
            billing_email="a@example.com",
            storage_prefix="tenants/a/",
        )
    )
    await sqlite_session.commit()

    sqlite_session.add(
        Tenant(
            id=uuid4(),
            slug="same",
            name="B",
            billing_email="b@example.com",
            storage_prefix="tenants/b/",
        )
    )
    with pytest.raises(IntegrityError):
        await sqlite_session.commit()


async def test_user_customer_and_contact_roundtrip(sqlite_session: AsyncSession) -> None:
    tenant = Tenant(
        id=uuid4(),
        slug="4mex",
        name="4MEX s.r.o.",
        billing_email="billing@4mex.cz",
        storage_prefix="tenants/4mex/",
    )
    sqlite_session.add(tenant)
    await sqlite_session.flush()

    # Tenant staff
    user = User(
        id=uuid4(),
        tenant_id=tenant.id,
        email="owner@4mex.cz",
        full_name="4MEX Owner",
        role=UserRole.TENANT_ADMIN,
        password_hash="fake-hash",
    )
    sqlite_session.add(user)

    # Customer company + contact
    customer = Customer(
        id=uuid4(),
        tenant_id=tenant.id,
        name="ACME s.r.o.",
        ico="12345678",
    )
    sqlite_session.add(customer)
    await sqlite_session.flush()

    contact = CustomerContact(
        id=uuid4(),
        tenant_id=tenant.id,
        customer_id=customer.id,
        email="jan@acme.cz",
        full_name="Jan Novák",
        role=CustomerContactRole.CUSTOMER_ADMIN,
    )
    sqlite_session.add(contact)
    await sqlite_session.commit()

    # Reload and sanity check
    loaded_user = (
        await sqlite_session.execute(select(User).where(User.email == "owner@4mex.cz"))
    ).scalar_one()
    assert loaded_user.role == UserRole.TENANT_ADMIN
    assert loaded_user.is_active is True
    assert loaded_user.session_version == 0
    assert loaded_user.notification_prefs == {}

    loaded_customer = (
        await sqlite_session.execute(select(Customer).where(Customer.ico == "12345678"))
    ).scalar_one()
    assert loaded_customer.name == "ACME s.r.o."

    loaded_contact = (
        await sqlite_session.execute(
            select(CustomerContact).where(CustomerContact.email == "jan@acme.cz")
        )
    ).scalar_one()
    assert loaded_contact.role == CustomerContactRole.CUSTOMER_ADMIN
    assert loaded_contact.customer_id == customer.id
    assert loaded_contact.tenant_id == tenant.id


async def test_user_email_unique_per_tenant(sqlite_session: AsyncSession) -> None:
    tenant = Tenant(
        id=uuid4(),
        slug="4mex",
        name="4MEX",
        billing_email="b@4mex.cz",
        storage_prefix="tenants/4mex/",
    )
    sqlite_session.add(tenant)
    await sqlite_session.flush()

    sqlite_session.add(
        User(
            id=uuid4(),
            tenant_id=tenant.id,
            email="duplicate@example.com",
            full_name="User A",
            role=UserRole.TENANT_STAFF,
        )
    )
    sqlite_session.add(
        User(
            id=uuid4(),
            tenant_id=tenant.id,
            email="duplicate@example.com",
            full_name="User B",
            role=UserRole.TENANT_STAFF,
        )
    )
    with pytest.raises(IntegrityError):
        await sqlite_session.commit()
