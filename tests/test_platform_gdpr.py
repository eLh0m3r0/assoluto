"""Platform Identity GDPR export + erasure (audit SEC-2 / F-12)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.audit_event import AuditEvent
from app.models.customer import Customer, CustomerContact
from app.models.enums import CustomerContactRole, UserRole
from app.models.tenant import Tenant
from app.models.user import User
from app.platform.models import Identity, TenantMembership
from app.security.passwords import hash_password
from tests import test_platform as _tp
from tests.test_platform import _platform_login

# Re-use the FEATURE_PLATFORM client fixtures.
platform_settings = _tp.platform_settings
platform_client = _tp.platform_client

pytestmark = pytest.mark.postgres


async def _seed(owner_engine, *, second_admin: bool) -> dict:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        tenant = Tenant(
            id=uuid4(),
            slug="gdpr-co",
            name="GDPR Co",
            billing_email="billing@gdpr.example",
            storage_prefix="tenants/gdpr-co/",
        )
        other = Tenant(
            id=uuid4(),
            slug="supplier",
            name="Supplier",
            billing_email="b@supplier.example",
            storage_prefix="tenants/supplier/",
        )
        session.add_all([tenant, other])
        await session.flush()
        identity = Identity(
            id=uuid4(),
            email="owner@gdpr.example",
            full_name="Olga Owner",
            password_hash=hash_password("ownerpass"),
            email_verified_at=datetime.now(UTC),
            terms_accepted_at=datetime.now(UTC),
            terms_accepted_ip="203.0.113.7",
        )
        user = User(
            id=uuid4(),
            tenant_id=tenant.id,
            email="owner@gdpr.example",
            full_name="Olga Owner",
            role=UserRole.TENANT_ADMIN,
            password_hash=hash_password("ownerpass"),
        )
        customer = Customer(id=uuid4(), tenant_id=other.id, name="GDPR Co as customer")
        session.add_all([identity, user, customer])
        await session.flush()
        contact = CustomerContact(
            id=uuid4(),
            tenant_id=other.id,
            customer_id=customer.id,
            email="owner@gdpr.example",
            full_name="Olga Owner",
            role=CustomerContactRole.CUSTOMER_ADMIN,
            password_hash=hash_password("ownerpass"),
            invited_at=datetime.now(UTC),
            accepted_at=datetime.now(UTC),
        )
        session.add(contact)
        await session.flush()
        session.add_all(
            [
                TenantMembership(
                    id=uuid4(), identity_id=identity.id, tenant_id=tenant.id, user_id=user.id
                ),
                TenantMembership(
                    id=uuid4(), identity_id=identity.id, tenant_id=other.id, contact_id=contact.id
                ),
            ]
        )
        if second_admin:
            session.add(
                User(
                    id=uuid4(),
                    tenant_id=tenant.id,
                    email="deputy@gdpr.example",
                    full_name="Deputy",
                    role=UserRole.TENANT_ADMIN,
                    password_hash=hash_password("x"),
                )
            )
    return {
        "identity_id": identity.id,
        "user_id": user.id,
        "contact_id": contact.id,
        "tenant_id": tenant.id,
        "other_id": other.id,
    }


async def test_identity_export_contains_profile_memberships_and_tenant_records(
    platform_client, owner_engine
) -> None:
    ids = await _seed(owner_engine, second_admin=False)
    await _platform_login(platform_client, "owner@gdpr.example", "ownerpass")

    page = await platform_client.get("/platform/profile")
    assert page.status_code == 200
    assert "/platform/profile/export" in page.text

    resp = await platform_client.get("/platform/profile/export")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    data = resp.json()
    assert data["kind"] == "identity"
    assert data["profile"]["email"] == "owner@gdpr.example"
    assert data["profile"]["terms_accepted_ip"] == "203.0.113.7"
    assert {m["tenant_id"] for m in data["memberships"]} == {
        str(ids["tenant_id"]),
        str(ids["other_id"]),
    }
    kinds = sorted(r["kind"] for r in data["tenant_records"])
    assert kinds == ["contact", "user"]
    assert "password_hash" not in resp.text


async def test_identity_export_requires_login(platform_client) -> None:
    resp = await platform_client.get(
        "/platform/profile/export", headers={"accept": "application/json"}
    )
    assert resp.status_code == 401


async def test_identity_erasure_refused_for_last_admin(platform_client, owner_engine) -> None:
    ids = await _seed(owner_engine, second_admin=False)
    await _platform_login(platform_client, "owner@gdpr.example", "ownerpass")

    resp = await platform_client.post(
        "/platform/profile/delete", data={"password": "ownerpass"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/platform/profile?error=")

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        identity = (
            await session.execute(select(Identity).where(Identity.id == ids["identity_id"]))
        ).scalar_one()
    assert identity.email == "owner@gdpr.example"
    assert identity.is_active


async def test_identity_erasure_wrong_password_is_refused(platform_client, owner_engine) -> None:
    await _seed(owner_engine, second_admin=True)
    await _platform_login(platform_client, "owner@gdpr.example", "ownerpass")
    resp = await platform_client.post(
        "/platform/profile/delete", data={"password": "nope"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]


async def test_identity_erasure_anonymises_everything_and_logs_out(
    platform_client, owner_engine
) -> None:
    ids = await _seed(owner_engine, second_admin=True)
    await _platform_login(platform_client, "owner@gdpr.example", "ownerpass")

    resp = await platform_client.post(
        "/platform/profile/delete", data={"password": "ownerpass"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/platform/login?notice=account_deleted"

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        identity = (
            await session.execute(select(Identity).where(Identity.id == ids["identity_id"]))
        ).scalar_one()
        user = (await session.execute(select(User).where(User.id == ids["user_id"]))).scalar_one()
        contact = (
            await session.execute(
                select(CustomerContact).where(CustomerContact.id == ids["contact_id"])
            )
        ).scalar_one()
        memberships = (
            (
                await session.execute(
                    select(TenantMembership).where(
                        TenantMembership.identity_id == ids["identity_id"]
                    )
                )
            )
            .scalars()
            .all()
        )
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.action.in_(["user.gdpr_erased", "contact.gdpr_erased"])
                    )
                )
            )
            .scalars()
            .all()
        )

    assert identity.email.endswith("@erased.invalid")
    assert identity.full_name != "Olga Owner"
    assert identity.password_hash == ""
    assert not identity.is_active
    assert identity.terms_accepted_ip is None
    assert identity.terms_accepted_at is not None  # accountability kept
    assert user.email.endswith("@erased.invalid") and not user.is_active
    assert contact.email.endswith("@erased.invalid") and not contact.is_active
    assert memberships and all(not m.is_active for m in memberships)
    # One erasure event per tenant record, none carrying the erased name/email.
    assert {e.tenant_id for e in events} == {ids["tenant_id"], ids["other_id"]}
    for e in events:
        assert "Olga" not in e.actor_label and "owner@" not in e.entity_label

    # The old login no longer works.
    login = await platform_client.post(
        "/platform/login",
        data={"email": "owner@gdpr.example", "password": "ownerpass"},
        follow_redirects=False,
    )
    assert login.status_code != 303
    notice = await platform_client.get("/platform/login?notice=account_deleted")
    assert notice.status_code == 200
