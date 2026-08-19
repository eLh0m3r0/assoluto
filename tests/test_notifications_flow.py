"""Tests for order notifications and the periodic auto-close job."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from freezegun import freeze_time
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.email.sender import CaptureSender
from app.models.customer import Customer, CustomerContact
from app.models.enums import CustomerContactRole, OrderStatus, UserRole
from app.models.order import Order
from app.models.user import User
from app.security.passwords import hash_password

pytestmark = pytest.mark.postgres


@pytest.fixture
def mock_s3(monkeypatch):  # type: ignore[misc]
    """In-process moto S3 with the bucket pre-created.

    Self-contained rather than autouse: it sets the env itself and
    rebuilds ``Settings`` before constructing the client, so only the
    test that asks for it is affected. The three older copies of this
    fixture (attachments / tenant-export / plan-e2e) pair a plain
    fixture with a file-wide autouse env fixture; consolidating all four
    into ``conftest.py`` is a separate cleanup — they disagree on bucket
    names.
    """
    import boto3
    from moto import mock_aws

    from app.config import get_settings
    from app.storage import s3 as s3_mod

    monkeypatch.setenv("S3_ENDPOINT_URL", "")
    monkeypatch.setenv("S3_ACCESS_KEY", "test")
    monkeypatch.setenv("S3_SECRET_KEY", "test")
    monkeypatch.setenv("S3_BUCKET", "portal-notif-test")
    get_settings.cache_clear()
    bucket = get_settings().s3_bucket

    with mock_aws():
        s3_mod.get_s3_client.cache_clear()
        boto3.client("s3", region_name="eu-central-1").create_bucket(
            Bucket=bucket,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )
        yield
        s3_mod.get_s3_client.cache_clear()


async def _seed(owner_engine, tenant_id: UUID) -> dict:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        staff = User(
            id=uuid4(),
            tenant_id=tenant_id,
            email="owner@4mex.cz",
            full_name="Owner",
            role=UserRole.TENANT_ADMIN,
            password_hash=hash_password("staffpass"),
        )
        customer = Customer(id=uuid4(), tenant_id=tenant_id, name="ACME", ico="11111111")
        session.add_all([staff, customer])
        await session.flush()
        contact = CustomerContact(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=customer.id,
            email="jan@acme.cz",
            full_name="Jan",
            role=CustomerContactRole.CUSTOMER_ADMIN,
            password_hash=hash_password("janpass"),
            invited_at=datetime.now(),
            accepted_at=datetime.now(),
        )
        session.add(contact)
        await session.flush()
        return {"staff": staff, "customer": customer, "contact": contact}


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


def _capture(client: AsyncClient) -> CaptureSender:
    capture = CaptureSender()
    client._transport.app.state.email_sender = capture  # type: ignore[attr-defined]
    return capture


async def test_submitting_order_emails_tenant_admin(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    capture = _capture(tenant_client)

    # Contact creates + submits.
    await _login(tenant_client, "jan@acme.cz", "janpass")
    create_resp = await tenant_client.post(
        "/app/orders", data={"title": "Nová zakázka"}, follow_redirects=False
    )
    order_id = UUID(create_resp.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0])
    await tenant_client.post(
        f"/app/orders/{order_id}/items",
        data={"description": "Řezání", "quantity": "5", "unit": "ks"},
        follow_redirects=False,
    )
    submit = await tenant_client.post(
        f"/app/orders/{order_id}/transitions/submitted", follow_redirects=False
    )
    assert submit.status_code == 303

    # One email to the tenant admin.
    assert len(capture.outbox) == 1
    msg = capture.outbox[0]
    assert msg.to == "owner@4mex.cz"
    assert "Nová objednávka" in msg.subject
    assert "ACME" in msg.subject
    assert "Nová zakázka" in msg.html
    assert f"/app/orders/{order_id}" in msg.html


async def test_staff_public_comment_emails_contact(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)

    await _login(tenant_client, "jan@acme.cz", "janpass")
    create_resp = await tenant_client.post(
        "/app/orders", data={"title": "Notif test"}, follow_redirects=False
    )
    order_id = UUID(create_resp.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0])

    # Swap to staff, install capture, add a public comment.
    await tenant_client.post("/auth/logout", follow_redirects=False)
    tenant_client.cookies.clear()
    await _login(tenant_client, "owner@4mex.cz", "staffpass")
    capture = _capture(tenant_client)

    resp = await tenant_client.post(
        f"/app/orders/{order_id}/comments",
        data={"body": "Zdravim, podivam se na to"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    assert len(capture.outbox) == 1
    msg = capture.outbox[0]
    assert msg.to == "jan@acme.cz"
    assert "Nový komentář" in msg.subject
    assert "Zdravim, podivam se na to" in msg.html


async def test_internal_comment_does_not_trigger_notification(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "jan@acme.cz", "janpass")
    create_resp = await tenant_client.post(
        "/app/orders", data={"title": "Silent comment"}, follow_redirects=False
    )
    order_id = UUID(create_resp.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0])

    await tenant_client.post("/auth/logout", follow_redirects=False)
    tenant_client.cookies.clear()
    await _login(tenant_client, "owner@4mex.cz", "staffpass")
    capture = _capture(tenant_client)

    resp = await tenant_client.post(
        f"/app/orders/{order_id}/comments",
        data={"body": "only for the team", "is_internal": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert capture.outbox == []


async def test_contact_comment_emails_tenant_admins(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    # Contact creates order + leaves a comment; staff gets an email.
    capture = _capture(tenant_client)

    await _login(tenant_client, "jan@acme.cz", "janpass")
    create_resp = await tenant_client.post(
        "/app/orders", data={"title": "Contact commented"}, follow_redirects=False
    )
    order_id = UUID(create_resp.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0])

    resp = await tenant_client.post(
        f"/app/orders/{order_id}/comments",
        data={"body": "otazka ke specifikaci"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    # Inbox: one email to owner@4mex.cz.
    admin_hits = [m for m in capture.outbox if m.to == "owner@4mex.cz"]
    assert len(admin_hits) == 1
    assert "Nový komentář" in admin_hits[0].subject


async def test_staff_quoting_emails_customer_contacts(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)

    await _login(tenant_client, "jan@acme.cz", "janpass")
    create_resp = await tenant_client.post(
        "/app/orders", data={"title": "Se cenou"}, follow_redirects=False
    )
    order_id = UUID(create_resp.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0])
    await tenant_client.post(
        f"/app/orders/{order_id}/items",
        data={"description": "A", "quantity": "1", "unit": "ks"},
        follow_redirects=False,
    )
    await tenant_client.post(
        f"/app/orders/{order_id}/transitions/submitted", follow_redirects=False
    )

    # Switch to staff, install capture AFTER submit so only the status-
    # change email from the upcoming transition is recorded.
    await _logout(tenant_client)
    await _login(tenant_client, "owner@4mex.cz", "staffpass")
    capture = _capture(tenant_client)

    # Give the priced line a price then transition to QUOTED.
    await tenant_client.post(
        f"/app/orders/{order_id}/items",
        data={"description": "Doprava", "quantity": "1", "unit": "ks", "unit_price": "500"},
        follow_redirects=False,
    )
    quoted = await tenant_client.post(
        f"/app/orders/{order_id}/transitions/quoted", follow_redirects=False
    )
    assert quoted.status_code == 303

    assert len(capture.outbox) == 1
    msg = capture.outbox[0]
    assert msg.to == "jan@acme.cz"
    # Email i18n refactor routes status_label through gettext so the
    # subject now carries "Naceněno" (status form) instead of
    # "Nacenění" (noun). Accept either to stay stable across further
    # catalogue edits.
    assert "Naceněno" in msg.subject or "Nacenění" in msg.subject
    assert "Se cenou" in msg.html


@pytest.mark.postgres
async def test_auto_close_delivered_orders_after_14_days(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)

    # Directly seed one DELIVERED order whose updated_at is 15 days old.
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        old_updated = datetime.now(UTC) - timedelta(days=15)
        order = Order(
            id=uuid4(),
            tenant_id=demo_tenant.id,
            customer_id=seed["customer"].id,
            number="2026-999001",
            title="Stale delivered",
            status=OrderStatus.DELIVERED,
            created_by_user_id=seed["staff"].id,
        )
        session.add(order)
        await session.flush()

        # Force updated_at back — the server default is now(), so UPDATE it.
        await session.execute(
            select(Order).where(Order.id == order.id).execution_options(synchronize_session=False)
        )
        from sqlalchemy import text

        await session.execute(
            text("UPDATE orders SET updated_at = :ts WHERE id = :id"),
            {"ts": old_updated, "id": order.id},
        )

    # Run the periodic task directly (same code path as APScheduler).
    from app.tasks.periodic import auto_close_delivered_orders

    with freeze_time(datetime.now(UTC)):
        closed = await auto_close_delivered_orders()
    assert closed == 1

    async with sm() as session:
        refreshed = (
            await session.execute(select(Order).where(Order.number == "2026-999001"))
        ).scalar_one()
    assert refreshed.status == OrderStatus.CLOSED
    assert refreshed.closed_at is not None


@pytest.mark.postgres
async def test_cleanup_stale_invited_contacts_removes_old_pending(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """Invited-but-never-accepted contacts older than 14 days get purged."""
    from sqlalchemy import select as _select
    from sqlalchemy import text as _text

    seed = await _seed(owner_engine, demo_tenant.id)

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    old_ts = datetime.now(UTC) - timedelta(days=20)
    fresh_ts = datetime.now(UTC)

    async with sm() as session, session.begin():
        session.add_all(
            [
                CustomerContact(
                    id=uuid4(),
                    tenant_id=demo_tenant.id,
                    customer_id=seed["customer"].id,
                    email="stale@acme.cz",
                    full_name="Stale",
                    role=CustomerContactRole.CUSTOMER_USER,
                    password_hash=None,
                    invited_at=fresh_ts,
                    accepted_at=None,
                ),
                CustomerContact(
                    id=uuid4(),
                    tenant_id=demo_tenant.id,
                    customer_id=seed["customer"].id,
                    email="recent@acme.cz",
                    full_name="Recent",
                    role=CustomerContactRole.CUSTOMER_USER,
                    password_hash=None,
                    invited_at=fresh_ts,
                    accepted_at=None,
                ),
            ]
        )
        await session.flush()

    # Backdate in a SEPARATE transaction so the ORM session's in-memory
    # state can't race the raw UPDATE.
    async with owner_engine.begin() as conn:
        await conn.execute(
            _text("UPDATE customer_contacts SET invited_at = :ts WHERE email = 'stale@acme.cz'"),
            {"ts": old_ts},
        )

    from app.tasks.periodic import cleanup_stale_invited_contacts

    removed = await cleanup_stale_invited_contacts()
    assert removed == 1

    async with sm() as session:
        emails = (
            (
                await session.execute(
                    _select(CustomerContact.email).where(
                        CustomerContact.tenant_id == demo_tenant.id
                    )
                )
            )
            .scalars()
            .all()
        )
    # "jan@acme.cz" (accepted) + "recent@acme.cz" remain; "stale@acme.cz" gone.
    assert "stale@acme.cz" not in emails
    assert "recent@acme.cz" in emails


@pytest.mark.postgres
async def test_cleanup_leaves_accepted_contacts_alone(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """Old but accepted contacts are never touched."""
    from sqlalchemy import text as _text

    seed = await _seed(owner_engine, demo_tenant.id)

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        session.add(
            CustomerContact(
                id=uuid4(),
                tenant_id=demo_tenant.id,
                customer_id=seed["customer"].id,
                email="veteran@acme.cz",
                full_name="Veteran",
                role=CustomerContactRole.CUSTOMER_USER,
                password_hash="fake",
                invited_at=datetime.now(UTC),
                accepted_at=datetime.now(UTC),
            )
        )

    async with owner_engine.begin() as conn:
        await conn.execute(
            _text("UPDATE customer_contacts SET invited_at = :ts WHERE email = 'veteran@acme.cz'"),
            {"ts": datetime.now(UTC) - timedelta(days=60)},
        )

    from app.tasks.periodic import cleanup_stale_invited_contacts

    removed = await cleanup_stale_invited_contacts()
    assert removed == 0


@pytest.mark.postgres
async def test_auto_close_does_not_touch_recent_orders(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        session.add(
            Order(
                id=uuid4(),
                tenant_id=demo_tenant.id,
                customer_id=seed["customer"].id,
                number="2026-999002",
                title="Recent delivered",
                status=OrderStatus.DELIVERED,
                created_by_user_id=seed["staff"].id,
            )
        )

    from app.tasks.periodic import auto_close_delivered_orders

    closed = await auto_close_delivered_orders()
    assert closed == 0


# ---------------------------------------------------------------------------
# Notification redesign (docs/NOTIFICATIONS_REDESIGN_2026-08-19.md)
# ---------------------------------------------------------------------------


async def _seed_with_operator(owner_engine, tenant_id: UUID) -> dict:
    """``_seed`` plus a ``tenant_staff`` Operator — the role that used to
    be filtered out of every recipient list."""
    seeded = await _seed(owner_engine, tenant_id)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        operator = User(
            id=uuid4(),
            tenant_id=tenant_id,
            email="operator@4mex.cz",
            full_name="Operátor",
            role=UserRole.TENANT_STAFF,
            password_hash=hash_password("operpass"),
        )
        session.add(operator)
        await session.flush()
    seeded["operator"] = operator
    return seeded


async def test_operator_is_emailed_about_a_submitted_order(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """End to end: the Operator now hears about incoming work."""
    await _seed_with_operator(owner_engine, demo_tenant.id)
    capture = _capture(tenant_client)

    await _login(tenant_client, "jan@acme.cz", "janpass")
    create = await tenant_client.post(
        "/app/orders", data={"title": "Frézování"}, follow_redirects=False
    )
    order_id = UUID(create.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0])
    submit = await tenant_client.post(
        f"/app/orders/{order_id}/transitions/submitted", follow_redirects=False
    )
    assert submit.status_code == 303

    assert {m.to for m in capture.outbox} == {"owner@4mex.cz", "operator@4mex.cz"}


async def test_staff_creating_an_order_tells_the_customer(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """Previously silent until the first status transition."""
    seeded = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "owner@4mex.cz", "staffpass")
    capture = _capture(tenant_client)

    resp = await tenant_client.post(
        "/app/orders",
        data={"title": "Objednávka po telefonu", "customer_id": str(seeded["customer"].id)},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    assert len(capture.outbox) == 1
    msg = capture.outbox[0]
    assert msg.to == "jan@acme.cz"
    assert "Objednávka po telefonu" in msg.html


async def test_contact_uploading_a_file_tells_the_supplier(
    tenant_client: AsyncClient, owner_engine, demo_tenant, mock_s3
) -> None:
    """A revised drawing nobody sees is scrap metal; this used to send
    nothing at all."""
    await _seed_with_operator(owner_engine, demo_tenant.id)
    await _login(tenant_client, "jan@acme.cz", "janpass")
    create = await tenant_client.post(
        "/app/orders", data={"title": "Výkres"}, follow_redirects=False
    )
    order_id = UUID(create.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0])

    capture = _capture(tenant_client)
    resp = await tenant_client.post(
        f"/app/orders/{order_id}/attachments",
        files={"file": ("vykres-rev-b.pdf", b"%PDF-1.4 fake", "application/pdf")},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text

    assert {m.to for m in capture.outbox} == {"owner@4mex.cz", "operator@4mex.cz"}
    assert "vykres-rev-b.pdf" in capture.outbox[0].html


async def test_assigning_an_order_emails_the_assignee(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    seeded = await _seed_with_operator(owner_engine, demo_tenant.id)
    await _login(tenant_client, "owner@4mex.cz", "staffpass")
    create = await tenant_client.post(
        "/app/orders",
        data={"title": "K přiřazení", "customer_id": str(seeded["customer"].id)},
        follow_redirects=False,
    )
    order_id = UUID(create.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0])

    capture = _capture(tenant_client)
    resp = await tenant_client.post(
        f"/app/orders/{order_id}/assign",
        data={"assigned_to": str(seeded["operator"].id)},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    assert [m.to for m in capture.outbox] == ["operator@4mex.cz"]

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        row = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
        assert row.assigned_to_user_id == seeded["operator"].id


async def test_assigning_to_yourself_sends_nothing(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """Taking a job off the pile is not news to the person who took it."""
    seeded = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "owner@4mex.cz", "staffpass")
    create = await tenant_client.post(
        "/app/orders",
        data={"title": "Beru si to", "customer_id": str(seeded["customer"].id)},
        follow_redirects=False,
    )
    order_id = UUID(create.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0])

    capture = _capture(tenant_client)
    resp = await tenant_client.post(
        f"/app/orders/{order_id}/assign",
        data={"assigned_to": str(seeded["staff"].id)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert capture.outbox == []


async def test_contacts_cannot_reach_the_assign_endpoint(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """Who is working on the job is the supplier's business."""
    seeded = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "jan@acme.cz", "janpass")
    create = await tenant_client.post("/app/orders", data={"title": "Cizí"}, follow_redirects=False)
    order_id = UUID(create.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0])

    resp = await tenant_client.post(
        f"/app/orders/{order_id}/assign",
        data={"assigned_to": str(seeded["staff"].id)},
        follow_redirects=False,
    )
    assert resp.status_code in (401, 403, 404)


async def test_saved_opt_out_actually_stops_the_email(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """The preferences page is wired to the router, not decorative.

    ``notification_prefs`` sat unread on both tables for the whole life
    of the product; this is the test that it is now load-bearing.
    """
    await _seed(owner_engine, demo_tenant.id)

    # The admin unticks "a customer submits an order" but keeps the rest.
    await _login(tenant_client, "owner@4mex.cz", "staffpass")
    saved = await tenant_client.post(
        "/app/admin/profile/notifications",
        data={
            "events": ["order_status_changed", "order_comment", "order_attachment"],
            "scope": "all",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    await _logout(tenant_client)

    await _login(tenant_client, "jan@acme.cz", "janpass")
    capture = _capture(tenant_client)
    create = await tenant_client.post(
        "/app/orders", data={"title": "Potichu"}, follow_redirects=False
    )
    order_id = UUID(create.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0])
    await tenant_client.post(
        f"/app/orders/{order_id}/transitions/submitted", follow_redirects=False
    )

    assert capture.outbox == [], "an opted-out event must never be re-added"


async def test_bulk_transition_sends_one_digest_per_recipient(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """Three orders moved in one click is one email, not three."""
    seeded = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "owner@4mex.cz", "staffpass")

    order_ids = []
    for i in range(3):
        create = await tenant_client.post(
            "/app/orders",
            data={"title": f"Dávka {i}", "customer_id": str(seeded["customer"].id)},
            follow_redirects=False,
        )
        order_ids.append(create.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0])

    capture = _capture(tenant_client)
    resp = await tenant_client.post(
        "/app/orders/bulk/transition",
        data={"order_ids": order_ids, "to_status": "confirmed"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text

    assert len(capture.outbox) == 1, "one digest, not one mail per order"
    msg = capture.outbox[0]
    assert msg.to == "jan@acme.cz"
    for order_id in order_ids:
        assert order_id in msg.html


# --- render smoke tests ----------------------------------------------------
# The preference macro dereferences its context variable, so a page that
# forgets to pass ``notification_prefs`` raises ``UndefinedError`` instead
# of rendering. These three GETs had no coverage at all before.


async def test_contact_profile_page_renders_preferences(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "jan@acme.cz", "janpass")

    resp = await tenant_client.get("/app/me/profile")
    assert resp.status_code == 200
    assert 'name="events"' in resp.text
    assert 'value="order_comment"' in resp.text
    # Staff-only events must not leak onto the customer's page.
    assert 'value="order_assigned"' not in resp.text


async def test_staff_profile_page_renders_preferences(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "owner@4mex.cz", "staffpass")

    resp = await tenant_client.get("/app/admin/profile")
    assert resp.status_code == 200
    assert 'value="order_assigned"' in resp.text


async def test_user_edit_page_renders_preferences(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    seeded = await _seed_with_operator(owner_engine, demo_tenant.id)
    await _login(tenant_client, "owner@4mex.cz", "staffpass")

    resp = await tenant_client.get(f"/app/admin/users/{seeded['operator'].id}/edit")
    assert resp.status_code == 200
    assert 'name="scope"' in resp.text


async def test_order_detail_shows_the_assignment_picker_only_to_staff(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    seeded = await _seed_with_operator(owner_engine, demo_tenant.id)
    await _login(tenant_client, "owner@4mex.cz", "staffpass")
    create = await tenant_client.post(
        "/app/orders",
        data={"title": "Viditelnost", "customer_id": str(seeded["customer"].id)},
        follow_redirects=False,
    )
    order_id = create.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0]

    staff_view = await tenant_client.get(f"/app/orders/{order_id}")
    assert staff_view.status_code == 200
    assert 'name="assigned_to"' in staff_view.text
    assert "operator@4mex.cz" not in staff_view.text, "emails stay off the page; names only"

    await _logout(tenant_client)
    await _login(tenant_client, "jan@acme.cz", "janpass")
    contact_view = await tenant_client.get(f"/app/orders/{order_id}")
    assert contact_view.status_code == 200
    assert 'name="assigned_to"' not in contact_view.text
    assert "Operátor" not in contact_view.text, "the supplier's staff list is internal"
