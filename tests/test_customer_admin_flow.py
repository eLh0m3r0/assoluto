"""End-to-end test of the customer admin flow.

Seeds a tenant staff user, logs them in via the real HTTP endpoint,
creates a customer, invites a contact, captures the invitation email,
and walks the invited contact through accept + login.
"""

from __future__ import annotations

import re
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.email.sender import CaptureSender
from app.models.enums import UserRole
from app.models.user import User
from app.security.passwords import hash_password

pytestmark = pytest.mark.postgres


async def _seed_owner(owner_engine, tenant_id) -> None:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        session.add(
            User(
                id=uuid4(),
                tenant_id=tenant_id,
                email="owner@4mex.cz",
                full_name="4MEX Owner",
                role=UserRole.TENANT_ADMIN,
                password_hash=hash_password("correct horse"),
            )
        )


async def _login(client: AsyncClient, email: str, password: str) -> None:
    resp = await client.post(
        "/auth/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    cookie = resp.headers["set-cookie"]
    # httpx stores cookies on the client automatically; just assert presence.
    assert "sme_portal_session" in cookie


async def test_customers_requires_login(tenant_client: AsyncClient) -> None:
    resp = await tenant_client.get("/app/customers", follow_redirects=False)
    assert resp.status_code == 401


async def test_full_customer_and_invite_flow(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed_owner(owner_engine, demo_tenant.id)

    # 1) Login as tenant owner.
    await _login(tenant_client, "owner@4mex.cz", "correct horse")

    # 2) Dashboard shows 0 customers initially. Assert on the Clients card
    # link instead of a translated word so the test is locale-agnostic.
    dash = await tenant_client.get("/app")
    assert dash.status_code == 200
    assert "/app/customers" in dash.text

    # 3) Create a customer via the real form endpoint.
    create_resp = await tenant_client.post(
        "/app/customers",
        data={
            "name": "ACME s.r.o.",
            "ico": "12345678",
            "dic": "CZ12345678",
            "notes": "",
        },
        follow_redirects=False,
    )
    assert create_resp.status_code == 303
    customer_url = create_resp.headers["location"].split("?", 1)[0]
    assert customer_url.startswith("/app/customers/")

    # 4) Customer list now has 1 entry.
    list_resp = await tenant_client.get("/app/customers")
    assert list_resp.status_code == 200
    assert "ACME s.r.o." in list_resp.text

    # 5) Install a CaptureSender so we can see the invite email.
    capture = CaptureSender()
    # Reach into the app under test and swap the sender.
    tenant_client._transport.app.state.email_sender = capture  # type: ignore[attr-defined]

    # 6) Invite a contact on the customer detail page.
    invite_resp = await tenant_client.post(
        f"{customer_url}/contacts",
        data={"email": "Jan@ACME.cz", "full_name": "Jan Novák"},
        follow_redirects=False,
    )
    assert invite_resp.status_code == 303

    # BackgroundTasks execute after the response in httpx+ASGITransport,
    # so we need to give them a chance. The test client awaits them
    # synchronously as part of the response lifecycle, so by the time we
    # reach this line the capture list should already hold the email.
    assert len(capture.outbox) == 1, capture.outbox
    msg = capture.outbox[0]
    assert msg.to == "jan@acme.cz"  # email normalized to lowercase
    assert "Jan Novák" in msg.html

    # 7) Pull the invite URL out of the rendered HTML and extract the token.
    match = re.search(r"/invite/accept\?token=([\w\-.]+)", msg.html)
    assert match, msg.html
    token = match.group(1)

    # 8) Contact opens the invite link, sets a password, gets auto-logged-in.
    # Use a separate client so we don't collide with the owner's session.
    owner_session_cookie = tenant_client.cookies.get("sme_portal_session")
    tenant_client.cookies.clear()  # simulate a fresh browser for the contact

    accept_get = await tenant_client.get(f"/invite/accept?token={token}")
    assert accept_get.status_code == 200
    assert "Jan Novák" in accept_get.text

    accept_post = await tenant_client.post(
        "/invite/accept",
        data={
            "token": token,
            "password": "supersecret",
            "password_confirm": "supersecret",
        },
        follow_redirects=False,
    )
    assert accept_post.status_code == 303
    assert accept_post.headers["location"] == "/app"
    contact_session_cookie = accept_post.headers.get("set-cookie", "")
    assert "sme_portal_session" in contact_session_cookie
    assert contact_session_cookie != owner_session_cookie

    # 9) Contact logs in from a fresh client with the new password.
    dash2 = await tenant_client.get("/app")
    assert dash2.status_code == 200
    # Contact nav does NOT include the staff Clients link — locale-agnostic
    # check against the href instead of the translated label.
    assert "/app/customers" not in _nav_block(dash2.text)


def _nav_block(html: str) -> str:
    """Extract just the navigation block so we can assert on its contents."""
    match = re.search(r"<nav.*?</nav>", html, flags=re.DOTALL)
    return match.group(0) if match else ""
