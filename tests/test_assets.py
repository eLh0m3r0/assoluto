"""Tests for asset management: staff CRUD, movement math, contact ACL."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.asset import Asset
from app.models.customer import Customer, CustomerContact
from app.models.enums import CustomerContactRole, UserRole
from app.models.user import User
from app.security.passwords import hash_password

pytestmark = pytest.mark.postgres


async def _seed(owner_engine, tenant_id: UUID) -> dict:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        staff = User(
            id=uuid4(),
            tenant_id=tenant_id,
            email="staff@4mex.cz",
            full_name="Staff",
            role=UserRole.TENANT_ADMIN,
            password_hash=hash_password("staffpass"),
        )
        acme = Customer(id=uuid4(), tenant_id=tenant_id, name="ACME", ico="11111111")
        other = Customer(id=uuid4(), tenant_id=tenant_id, name="Other", ico="22222222")
        session.add_all([staff, acme, other])
        await session.flush()

        jan = CustomerContact(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=acme.id,
            email="jan@acme.cz",
            full_name="Jan",
            role=CustomerContactRole.CUSTOMER_USER,
            password_hash=hash_password("janpass"),
            invited_at=datetime.now(),
            accepted_at=datetime.now(),
        )
        eva = CustomerContact(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=other.id,
            email="eva@other.cz",
            full_name="Eva",
            role=CustomerContactRole.CUSTOMER_USER,
            password_hash=hash_password("evapass"),
            invited_at=datetime.now(),
            accepted_at=datetime.now(),
        )
        session.add_all([jan, eva])
        await session.flush()

        return {
            "staff": staff,
            "acme": acme,
            "other": other,
            "jan": jan,
            "eva": eva,
        }


async def _login(client: AsyncClient, email: str, password: str) -> None:
    resp = await client.post(
        "/auth/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


async def _logout(client: AsyncClient) -> None:
    await client.post("/auth/logout", follow_redirects=False)
    client.cookies.clear()


async def _fresh_asset(owner_engine, asset_id) -> Asset:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        return (await session.execute(select(Asset).where(Asset.id == asset_id))).scalar_one()


async def test_staff_creates_asset_and_runs_movements(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)

    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    create_resp = await tenant_client.post(
        "/app/assets",
        data={
            "customer_id": str(seed["acme"].id),
            "code": "AL-2MM",
            "name": "Plech Al 2mm",
            "unit": "kg",
        },
        follow_redirects=False,
    )
    assert create_resp.status_code == 303
    asset_url = create_resp.headers["location"]
    asset_id = UUID(asset_url.rsplit("/", 1)[-1])

    # receive 100
    await tenant_client.post(
        f"/app/assets/{asset_id}/movements",
        data={"type": "receive", "quantity": "100"},
        follow_redirects=False,
    )
    # issue 30
    await tenant_client.post(
        f"/app/assets/{asset_id}/movements",
        data={"type": "issue", "quantity": "30"},
        follow_redirects=False,
    )
    # consume 20
    await tenant_client.post(
        f"/app/assets/{asset_id}/movements",
        data={"type": "consume", "quantity": "20"},
        follow_redirects=False,
    )

    asset = await _fresh_asset(owner_engine, asset_id)
    assert asset.current_quantity == Decimal("50")


async def test_issue_beyond_stock_is_rejected(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")

    create_resp = await tenant_client.post(
        "/app/assets",
        data={
            "customer_id": str(seed["acme"].id),
            "code": "STK",
            "name": "Stock",
            "unit": "ks",
        },
        follow_redirects=False,
    )
    asset_id = UUID(create_resp.headers["location"].rsplit("/", 1)[-1])

    # receive 5
    await tenant_client.post(
        f"/app/assets/{asset_id}/movements",
        data={"type": "receive", "quantity": "5"},
        follow_redirects=False,
    )
    # issue 10 -> 409
    resp = await tenant_client.post(
        f"/app/assets/{asset_id}/movements",
        data={"type": "issue", "quantity": "10"},
        follow_redirects=False,
    )
    assert resp.status_code == 409

    asset = await _fresh_asset(owner_engine, asset_id)
    assert asset.current_quantity == Decimal("5")


async def test_contact_sees_only_own_customer_assets(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)

    # Staff seeds one asset for ACME and one for Other.
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    for code, cust_id in (
        ("ACME-A", seed["acme"].id),
        ("OTH-A", seed["other"].id),
    ):
        await tenant_client.post(
            "/app/assets",
            data={
                "customer_id": str(cust_id),
                "code": code,
                "name": code,
                "unit": "ks",
            },
            follow_redirects=False,
        )

    # Jan (ACME contact) logs in and lists.
    await _logout(tenant_client)
    await _login(tenant_client, "jan@acme.cz", "janpass")

    listing = await tenant_client.get("/app/assets")
    assert listing.status_code == 200
    assert "ACME-A" in listing.text
    assert "OTH-A" not in listing.text

    # Fetch the OTH-A id via an owner session.
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        oth = (await session.execute(select(Asset).where(Asset.code == "OTH-A"))).scalar_one()

    # Direct access -> 404.
    direct = await tenant_client.get(f"/app/assets/{oth.id}")
    assert direct.status_code == 404


async def test_contact_cannot_create_or_add_movements(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)

    # Staff creates an ACME asset first.
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    create_resp = await tenant_client.post(
        "/app/assets",
        data={
            "customer_id": str(seed["acme"].id),
            "code": "X",
            "name": "X",
            "unit": "ks",
        },
        follow_redirects=False,
    )
    asset_id = UUID(create_resp.headers["location"].rsplit("/", 1)[-1])

    # Jan tries to create + post movements -> 403.
    await _logout(tenant_client)
    await _login(tenant_client, "jan@acme.cz", "janpass")

    new = await tenant_client.post(
        "/app/assets",
        data={
            "customer_id": str(seed["acme"].id),
            "code": "Y",
            "name": "Y",
            "unit": "ks",
        },
        follow_redirects=False,
    )
    assert new.status_code == 403

    mv = await tenant_client.post(
        f"/app/assets/{asset_id}/movements",
        data={"type": "receive", "quantity": "1"},
        follow_redirects=False,
    )
    assert mv.status_code == 403
