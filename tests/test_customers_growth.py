"""Customer page + lifecycle: orders on detail, last sign-in, archive, team.

UX-13 (orders + "New order for this client"), UX-14 (last_login_at),
LOGIC-15 (archive / block a customer), IDEA-9 (client admins invite
colleagues), MKT-9 (portal footer for contacts).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.email.sender import CaptureSender
from app.models.customer import Customer, CustomerContact
from app.models.enums import CustomerContactRole, UserRole
from app.models.order import Order
from app.models.user import User
from app.security.passwords import hash_password

pytestmark = pytest.mark.postgres

PASSWORD = "correct horse battery"


async def _seed(owner_engine, tenant_id) -> dict:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    now = datetime.now(UTC)
    async with sm() as session, session.begin():
        session.add(
            User(
                id=uuid4(),
                tenant_id=tenant_id,
                email="owner@4mex.cz",
                full_name="Owner",
                role=UserRole.TENANT_ADMIN,
                password_hash=hash_password(PASSWORD),
            )
        )
        customer = Customer(id=uuid4(), tenant_id=tenant_id, name="Strojírna Ukázková s.r.o.")
        other = Customer(id=uuid4(), tenant_id=tenant_id, name="Kovovýroba Vzorová a.s.")
        session.add_all([customer, other])
        await session.flush()
        admin = CustomerContact(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=customer.id,
            email="sefka@ukazkova.cz",
            full_name="Šéfka Nákupu",
            role=CustomerContactRole.CUSTOMER_ADMIN,
            password_hash=hash_password(PASSWORD),
            invited_at=now - timedelta(days=10),
            accepted_at=now - timedelta(days=9),
            last_login_at=now - timedelta(days=3),
        )
        user = CustomerContact(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=customer.id,
            email="technik@ukazkova.cz",
            full_name="Technik Výkresů",
            role=CustomerContactRole.CUSTOMER_USER,
            password_hash=hash_password(PASSWORD),
            invited_at=now - timedelta(days=10),
            accepted_at=now - timedelta(days=9),
        )
        session.add_all([admin, user])
        for i in range(12):
            session.add(
                Order(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    customer_id=customer.id,
                    number=f"2026-{i:06d}",
                    title=f"Díl {i}",
                    created_at=now - timedelta(days=i),
                )
            )
        session.add(
            Order(
                id=uuid4(),
                tenant_id=tenant_id,
                customer_id=other.id,
                number="2026-999999",
                title="Cizí zakázka",
            )
        )
    return {"customer": customer, "other": other, "admin": admin, "user": user}


async def _login(client: AsyncClient, email: str) -> None:
    client.cookies.clear()
    resp = await client.post(
        "/auth/login", data={"email": email, "password": PASSWORD}, follow_redirects=False
    )
    assert resp.status_code == 303, resp.text


# ----------------------------------------------------------- detail page


async def test_detail_lists_recent_orders_and_new_order_cta(
    tenant_client, owner_engine, demo_tenant
) -> None:
    seeded = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "owner@4mex.cz")
    cid = seeded["customer"].id

    resp = await tenant_client.get(f"/app/customers/{cid}")
    assert resp.status_code == 200
    assert f"/app/orders/new?customer={cid}" in resp.text
    assert f"/app/orders?customer={cid}" in resp.text
    assert "(12)" in resp.text  # total in the "all orders" link
    assert resp.text.count('href="/app/orders/') - resp.text.count("/app/orders/new") == 10
    assert "Cizí zakázka" not in resp.text  # another customer's order

    form = await tenant_client.get(f"/app/orders/new?customer={cid}")
    assert form.status_code == 200
    assert re.search(rf'value="{cid}"\s+selected', form.text)


async def test_detail_shows_last_sign_in_per_contact(
    tenant_client, owner_engine, demo_tenant
) -> None:
    seeded = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "owner@4mex.cz")
    resp = await tenant_client.get(f"/app/customers/{seeded['customer'].id}")
    # The admin signed in 3 days ago; the technician never did.
    assert resp.text.count("<time datetime=") == 1
    assert "Never signed in" in resp.text or "Zatím bez přihlášení" in resp.text
    listing = await tenant_client.get("/app/customers")
    assert "Strojírna Ukázková s.r.o." in listing.text


async def test_duplicate_contact_invite_rerenders_with_error(
    tenant_client, owner_engine, demo_tenant
) -> None:
    seeded = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "owner@4mex.cz")
    tenant_client._transport.app.state.email_sender = CaptureSender()  # type: ignore[attr-defined]
    resp = await tenant_client.post(
        f"/app/customers/{seeded['customer'].id}/contacts",
        data={"email": "technik@ukazkova.cz", "full_name": "Dup"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "Technik Výkresů" in resp.text  # page re-rendered with contacts


async def test_staff_can_invite_a_client_admin(tenant_client, owner_engine, demo_tenant) -> None:
    seeded = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "owner@4mex.cz")
    tenant_client._transport.app.state.email_sender = CaptureSender()  # type: ignore[attr-defined]
    resp = await tenant_client.post(
        f"/app/customers/{seeded['customer'].id}/contacts",
        data={"email": "novy@ukazkova.cz", "full_name": "Nový", "role": "customer_admin"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    async with owner_engine.connect() as conn:
        role = (
            await conn.execute(
                text("SELECT role FROM customer_contacts WHERE email = 'novy@ukazkova.cz'")
            )
        ).scalar_one()
    assert role.lower() == "customer_admin"  # stored by enum name


# --------------------------------------------------------------- archive


async def test_archive_blocks_contacts_and_hides_customer(
    tenant_client, owner_engine, demo_tenant
) -> None:
    seeded = await _seed(owner_engine, demo_tenant.id)
    cid = seeded["customer"].id

    # A contact already signed in…
    await _login(tenant_client, "technik@ukazkova.cz")
    contact_cookie = tenant_client.cookies.get("sme_portal_session")
    assert (await tenant_client.get("/app")).status_code == 200

    # …then the supplier archives the customer.
    await _login(tenant_client, "owner@4mex.cz")
    resp = await tenant_client.post(f"/app/customers/{cid}/archive", follow_redirects=False)
    assert resp.status_code == 303
    assert "notice=" in resp.headers["location"]

    # Hidden from the order-form picker, listed under archived.
    form = await tenant_client.get("/app/orders/new")
    assert str(cid) not in form.text
    listing = await tenant_client.get("/app/customers")
    assert f"/app/customers/{cid}" in listing.text  # archived section link

    # Staff cannot start new work for it.
    create = await tenant_client.post(
        "/app/orders", data={"title": "Nová", "customer_id": str(cid)}, follow_redirects=False
    )
    assert create.status_code in (400, 404)

    # The existing contact session is dead…
    tenant_client.cookies.clear()
    tenant_client.cookies.set("sme_portal_session", contact_cookie)
    dead = await tenant_client.get("/app", follow_redirects=False)
    assert dead.status_code in (303, 401)
    # …and a fresh login is refused.
    tenant_client.cookies.clear()
    login = await tenant_client.post(
        "/auth/login",
        data={"email": "technik@ukazkova.cz", "password": PASSWORD},
        follow_redirects=False,
    )
    assert login.status_code == 403

    # Data kept.
    async with owner_engine.connect() as conn:
        n_orders = (
            await conn.execute(
                text("SELECT count(*) FROM orders WHERE customer_id = :c"), {"c": cid}
            )
        ).scalar_one()
        actions = {
            row[0]
            for row in (
                await conn.execute(
                    text("SELECT action FROM audit_events WHERE entity_id = :c"), {"c": cid}
                )
            ).all()
        }
    assert n_orders == 12
    assert "customer.archived" in actions

    # Unarchive restores sign-in.
    await _login(tenant_client, "owner@4mex.cz")
    resp = await tenant_client.post(f"/app/customers/{cid}/unarchive", follow_redirects=False)
    assert resp.status_code == 303
    await _login(tenant_client, "technik@ukazkova.cz")
    assert (await tenant_client.get("/app")).status_code == 200


async def test_archived_customer_cannot_get_new_contacts(
    tenant_client, owner_engine, demo_tenant
) -> None:
    seeded = await _seed(owner_engine, demo_tenant.id)
    cid = seeded["customer"].id
    await _login(tenant_client, "owner@4mex.cz")
    await tenant_client.post(f"/app/customers/{cid}/archive", follow_redirects=False)
    resp = await tenant_client.post(
        f"/app/customers/{cid}/contacts",
        data={"email": "x@ukazkova.cz", "full_name": "X"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]


async def test_archived_customer_contacts_get_no_order_mail(
    owner_engine, demo_tenant, settings
) -> None:
    from app.models.enums import OrderStatus
    from app.services.notification_service import build_order_status_changed

    seeded = await _seed(owner_engine, demo_tenant.id)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        await session.execute(
            text("UPDATE customers SET is_active = false WHERE id = :c"),
            {"c": seeded["customer"].id},
        )
    async with sm() as session:
        order = (
            await session.execute(
                select(Order).where(Order.customer_id == seeded["customer"].id).limit(1)
            )
        ).scalar_one()
        payloads = await build_order_status_changed(
            session,
            tenant=demo_tenant,
            order=order,
            to_status=OrderStatus.READY,
            base_url="https://4mex.example",
            settings=settings,
        )
    assert payloads == []


# ------------------------------------------------------ client admin team


async def test_team_page_is_client_admin_only(tenant_client, owner_engine, demo_tenant) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "technik@ukazkova.cz")
    assert (await tenant_client.get("/app/me/team", follow_redirects=False)).status_code == 403
    await _login(tenant_client, "owner@4mex.cz")
    assert (await tenant_client.get("/app/me/team", follow_redirects=False)).status_code == 403
    await _login(tenant_client, "sefka@ukazkova.cz")
    resp = await tenant_client.get("/app/me/team")
    assert resp.status_code == 200
    assert "Technik Výkresů" in resp.text
    dash = await tenant_client.get("/app")
    assert "/app/me/team" in dash.text  # nav entry for client admins


async def test_client_admin_invites_colleague(tenant_client, owner_engine, demo_tenant) -> None:
    seeded = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "sefka@ukazkova.cz")
    capture = CaptureSender()
    tenant_client._transport.app.state.email_sender = capture  # type: ignore[attr-defined]

    resp = await tenant_client.post(
        "/app/me/team/invite",
        data={"email": "Kolega@Ukazkova.cz", "full_name": "Kolega Nový"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "notice=" in resp.headers["location"]
    assert [m.to for m in capture.outbox] == ["kolega@ukazkova.cz"]
    assert "/invite/accept?token=" in capture.outbox[0].text

    async with owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT customer_id, role FROM customer_contacts "
                    "WHERE email = 'kolega@ukazkova.cz'"
                )
            )
        ).one()
        audit = (
            await conn.execute(
                text(
                    "SELECT actor_type FROM audit_events WHERE action = 'customer_contact.invited'"
                )
            )
        ).scalar_one()
    assert UUID(str(row[0])) == seeded["customer"].id
    assert row[1].lower() == "customer_user"  # never escalates to admin
    assert audit == "contact"

    # Duplicate → flash error, no second mail.
    dup = await tenant_client.post(
        "/app/me/team/invite",
        data={"email": "kolega@ukazkova.cz", "full_name": "Again"},
        follow_redirects=False,
    )
    assert "error=" in dup.headers["location"]
    assert len(capture.outbox) == 1


async def test_client_admin_invite_respects_plan_limit(
    tenant_client, owner_engine, demo_tenant
) -> None:
    from app.platform.billing.models import Plan, Subscription

    await _seed(owner_engine, demo_tenant.id)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        plan = Plan(id=uuid4(), code=f"cap-{uuid4().hex[:6]}", name="Cap", max_contacts=2)
        session.add(plan)
        await session.flush()
        session.add(
            Subscription(id=uuid4(), tenant_id=demo_tenant.id, plan_id=plan.id, status="active")
        )
    await _login(tenant_client, "sefka@ukazkova.cz")
    tenant_client._transport.app.state.email_sender = CaptureSender()  # type: ignore[attr-defined]
    resp = await tenant_client.post(
        "/app/me/team/invite",
        data={"email": "treti@ukazkova.cz", "full_name": "Třetí"},
        follow_redirects=False,
    )
    assert resp.status_code == 402


async def test_client_admin_invite_requires_csrf(tenant_client, owner_engine, demo_tenant) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "sefka@ukazkova.cz")
    from httpx import AsyncClient as RawClient

    raw = RawClient(
        transport=tenant_client._transport,  # type: ignore[attr-defined]
        base_url="http://testserver",
        headers={"X-Tenant-Slug": demo_tenant.slug},
        cookies=tenant_client.cookies,
    )
    resp = await raw.post(
        "/app/me/team/invite",
        data={"email": "x@ukazkova.cz", "full_name": "X"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


# ------------------------------------------------------- portal footer


async def test_portal_footer_shown_to_contacts_only(
    tenant_client, owner_engine, demo_tenant, settings
) -> None:
    settings.platform_cookie_domain = ".assoluto.test"
    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "technik@ukazkova.cz")
    resp = await tenant_client.get("/app")
    assert "https://assoluto.test/?ref=portal&amp;t=4mex" in resp.text
    await _login(tenant_client, "owner@4mex.cz")
    resp = await tenant_client.get("/app")
    assert "ref=portal" not in resp.text


async def test_platform_switcher_treats_archived_customer_contact_as_blocked(
    owner_engine, demo_tenant
) -> None:
    from app.platform.routers.platform_auth import _contact_customer_archived

    seeded = await _seed(owner_engine, demo_tenant.id)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        contact = (
            await session.execute(
                select(CustomerContact).where(CustomerContact.id == seeded["user"].id)
            )
        ).scalar_one()
        assert await _contact_customer_archived(session, contact) is False
        await session.execute(
            text("UPDATE customers SET is_active = false WHERE id = :c"),
            {"c": seeded["customer"].id},
        )
        assert await _contact_customer_archived(session, contact) is True
        staff = (await session.execute(select(User))).scalars().first()
        assert await _contact_customer_archived(session, staff) is False
