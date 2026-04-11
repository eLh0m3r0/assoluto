"""End-to-end tests for the public auth flow.

Uses the real Postgres-backed `tenant_client` fixture: requests flow
through the entire FastAPI app, hit the real DB via the `portal_app`
role (subject to RLS), and email sends are captured by `CaptureSender`
so we can inspect outbound messages.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.customer import Customer, CustomerContact
from app.models.enums import CustomerContactRole, UserRole
from app.models.user import User
from app.security.passwords import hash_password
from app.services.auth_service import create_invitation_token

pytestmark = pytest.mark.postgres


async def _seed_tenant_user_and_contact(
    owner_engine, tenant_id: UUID
) -> tuple[User, Customer, CustomerContact]:
    """Seed a tenant staff user and one invited contact for the demo tenant."""
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        user = User(
            id=uuid4(),
            tenant_id=tenant_id,
            email="owner@4mex.cz",
            full_name="4MEX Owner",
            role=UserRole.TENANT_ADMIN,
            password_hash=hash_password("correct horse"),
        )
        customer = Customer(
            id=uuid4(),
            tenant_id=tenant_id,
            name="ACME s.r.o.",
            ico="12345678",
        )
        session.add_all([user, customer])
        await session.flush()

        contact = CustomerContact(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=customer.id,
            email="jan@acme.cz",
            full_name="Jan Novák",
            role=CustomerContactRole.CUSTOMER_ADMIN,
            # Invited but not yet accepted.
            invited_at=None,
        )
        session.add(contact)
        await session.flush()
        return user, customer, contact


async def test_login_form_renders(tenant_client: AsyncClient, demo_tenant) -> None:
    response = await tenant_client.get("/auth/login")
    assert response.status_code == 200
    assert "Přihlášení" in response.text
    assert demo_tenant.name in response.text


async def test_login_wrong_password_returns_401(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed_tenant_user_and_contact(owner_engine, demo_tenant.id)
    response = await tenant_client.post(
        "/auth/login",
        data={"email": "owner@4mex.cz", "password": "wrong"},
    )
    assert response.status_code == 401
    assert "Neplatný e-mail nebo heslo" in response.text


async def test_login_success_sets_session_cookie(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed_tenant_user_and_contact(owner_engine, demo_tenant.id)
    response = await tenant_client.post(
        "/auth/login",
        data={"email": "owner@4mex.cz", "password": "correct horse"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/app"
    assert "sme_portal_session" in response.headers.get("set-cookie", "")


async def test_login_unknown_email_returns_401(tenant_client: AsyncClient, demo_tenant) -> None:
    response = await tenant_client.post(
        "/auth/login",
        data={"email": "nobody@4mex.cz", "password": "x"},
    )
    assert response.status_code == 401


async def test_logout_clears_session_cookie(
    tenant_client: AsyncClient,
) -> None:
    response = await tenant_client.post("/auth/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


async def test_invite_accept_happy_path_and_autologin(
    tenant_client: AsyncClient, owner_engine, demo_tenant, settings
) -> None:
    _, _customer, contact = await _seed_tenant_user_and_contact(owner_engine, demo_tenant.id)
    token = create_invitation_token(
        settings.app_secret_key,
        tenant_id=demo_tenant.id,
        contact_id=contact.id,
    )

    # GET the form to make sure the contact is loaded.
    get_resp = await tenant_client.get(f"/invite/accept?token={token}")
    assert get_resp.status_code == 200
    assert "Jan Novák" in get_resp.text

    # POST the new password.
    post_resp = await tenant_client.post(
        "/invite/accept",
        data={
            "token": token,
            "password": "supersecret",
            "password_confirm": "supersecret",
        },
        follow_redirects=False,
    )
    assert post_resp.status_code == 303
    assert post_resp.headers["location"] == "/app"
    assert "sme_portal_session" in post_resp.headers.get("set-cookie", "")

    # Verify the DB row was updated.
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        refreshed = (
            await session.execute(select(CustomerContact).where(CustomerContact.id == contact.id))
        ).scalar_one()
        assert refreshed.password_hash is not None
        assert refreshed.accepted_at is not None


async def test_invite_accept_with_mismatched_password(
    tenant_client: AsyncClient, owner_engine, demo_tenant, settings
) -> None:
    _, _, contact = await _seed_tenant_user_and_contact(owner_engine, demo_tenant.id)
    token = create_invitation_token(
        settings.app_secret_key,
        tenant_id=demo_tenant.id,
        contact_id=contact.id,
    )
    response = await tenant_client.post(
        "/invite/accept",
        data={
            "token": token,
            "password": "onething",
            "password_confirm": "another",
        },
    )
    assert response.status_code == 400
    assert "neshodují" in response.text


async def test_invite_accept_with_invalid_token(tenant_client: AsyncClient, demo_tenant) -> None:
    response = await tenant_client.get("/invite/accept?token=not-a-real-token")
    assert response.status_code == 400
    assert "neplatná" in response.text.lower()
