"""Regression tests for the 2026-10-03 audit — the supplier's billing
state stays the supplier's business (T6: LOGIC-3, BIZ-13), trial
countdown (LOGIC-4) and plan-limit integrity (T13: LOGIC-8/9/20,
Codex-9/10/11).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.customer import Customer, CustomerContact
from app.models.enums import CustomerContactRole, UserRole
from app.models.order import Order
from app.models.user import User
from app.platform.billing.models import Plan, Subscription
from app.security.passwords import hash_password

pytestmark = pytest.mark.postgres

NOW = datetime.now(UTC)


async def _seed_people(owner_engine, tenant_id) -> dict:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s, s.begin():
        admin = User(
            id=uuid4(),
            tenant_id=tenant_id,
            email="admin@4mex.cz",
            full_name="Admin",
            role=UserRole.TENANT_ADMIN,
            password_hash=hash_password("adminpass"),
        )
        operator = User(
            id=uuid4(),
            tenant_id=tenant_id,
            email="operator@4mex.cz",
            full_name="Operator",
            role=UserRole.TENANT_STAFF,
            password_hash=hash_password("operatorpass"),
        )
        customer = Customer(id=uuid4(), tenant_id=tenant_id, name="ACME", ico="11111111")
        s.add_all([admin, operator, customer])
        await s.flush()
        contact = CustomerContact(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=customer.id,
            email="jan@acme.cz",
            full_name="Jan",
            role=CustomerContactRole.CUSTOMER_ADMIN,
            password_hash=hash_password("contactpass"),
            invited_at=NOW,
            accepted_at=NOW,
        )
        s.add(contact)
    return {"admin": admin, "operator": operator, "customer": customer, "contact": contact}


async def _set_subscription(owner_engine, tenant_id, *, plan_code="starter", **fields) -> None:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s, s.begin():
        await s.execute(delete(Subscription).where(Subscription.tenant_id == tenant_id))
        plan = (await s.execute(select(Plan).where(Plan.code == plan_code))).scalar_one()
        sub = Subscription(tenant_id=tenant_id, plan_id=plan.id, status=fields.pop("status"))
        for k, v in fields.items():
            setattr(sub, k, v)
        s.add(sub)


async def _login(client, email: str, password: str) -> None:
    client.cookies.clear()
    resp = await client.post(
        "/auth/login", data={"email": email, "password": password}, follow_redirects=False
    )
    assert resp.status_code == 303, resp.text


# ---------------------------------------------------- LOGIC-3 / BIZ-13 banners


@pytest.mark.parametrize("status", ["past_due", "canceled", "unpaid"])
async def test_contacts_never_see_supplier_billing_banners(
    tenant_client, owner_engine, demo_tenant, settings, status
) -> None:
    settings.feature_platform = True
    await _seed_people(owner_engine, demo_tenant.id)
    await _set_subscription(
        owner_engine, demo_tenant.id, status=status, current_period_end=NOW + timedelta(days=5)
    )

    await _login(tenant_client, "jan@acme.cz", "contactpass")
    page = await tenant_client.get("/app")
    assert page.status_code == 200
    for needle in ("Payment overdue", "Subscription ended", "Stripe", "/platform/billing"):
        assert needle not in page.text, needle

    await _login(tenant_client, "admin@4mex.cz", "adminpass")
    page = await tenant_client.get("/app")
    assert "/platform/billing" in page.text  # admins keep the CTA


async def test_operator_sees_neutral_line_without_billing_link(
    tenant_client, owner_engine, demo_tenant, settings
) -> None:
    settings.feature_platform = True
    await _seed_people(owner_engine, demo_tenant.id)
    await _set_subscription(owner_engine, demo_tenant.id, status="past_due")
    await _login(tenant_client, "operator@4mex.cz", "operatorpass")
    page = await tenant_client.get("/app")
    assert 'role="alert"' in page.text
    assert "check your payment method" not in page.text
    assert 'href="/platform/billing"' not in page.text


async def test_canceled_banner_shows_hard_cut_date(
    tenant_client, owner_engine, demo_tenant, settings
) -> None:
    settings.feature_platform = True
    await _seed_people(owner_engine, demo_tenant.id)
    period_end = NOW + timedelta(days=2)
    await _set_subscription(
        owner_engine, demo_tenant.id, status="canceled", current_period_end=period_end
    )
    await _login(tenant_client, "admin@4mex.cz", "adminpass")
    page = await tenant_client.get("/app")
    assert (period_end + timedelta(days=3)).strftime("%d.%m.%Y") in page.text


async def test_trial_countdown_for_admin_only(
    tenant_client, owner_engine, demo_tenant, settings
) -> None:
    """LOGIC-4: the in-app UI never mentioned the trial."""
    settings.feature_platform = True
    await _seed_people(owner_engine, demo_tenant.id)
    ends = NOW + timedelta(days=5, hours=2)
    await _set_subscription(owner_engine, demo_tenant.id, status="trialing", trial_ends_at=ends)

    await _login(tenant_client, "admin@4mex.cz", "adminpass")
    page = await tenant_client.get("/app")
    assert ends.strftime("%d.%m.%Y") in page.text
    assert "bg-amber-50" in page.text  # ≤ 7 days left → amber

    await _login(tenant_client, "operator@4mex.cz", "operatorpass")
    page = await tenant_client.get("/app")
    assert ends.strftime("%d.%m.%Y") not in page.text

    await _login(tenant_client, "jan@acme.cz", "contactpass")
    page = await tenant_client.get("/app")
    assert ends.strftime("%d.%m.%Y") not in page.text


async def test_deactivated_tenant_serves_neutral_unavailable_page(
    tenant_client, owner_engine, demo_tenant
) -> None:
    """A hard-cut tenant used to be a bare 404 for its customers."""
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE tenants SET is_active = false WHERE id = :id"), {"id": demo_tenant.id}
        )
    for path in ("/auth/login", "/app", "/"):
        resp = await tenant_client.get(path, headers={"accept": "text/html"})
        assert resp.status_code == 503, path
        assert demo_tenant.name in resp.text
        for word in ("subscription", "Stripe", "plan", "payment", "předplatné"):
            assert word not in resp.text.lower().replace("platform", ""), word


async def test_unknown_tenant_is_still_404(client) -> None:
    resp = await client.get("/app", headers={"X-Tenant-Slug": "does-not-exist"})
    assert resp.status_code == 404


# ------------------------------------------- LOGIC-3 soft limit for contacts


@pytest.fixture
def captured_alerts(monkeypatch):
    """Run the admin alert synchronously into a CaptureSender."""
    from app.email.sender import CaptureSender
    from app.platform import usage

    sender = CaptureSender()
    monkeypatch.setattr(usage, "_alert_sender", lambda settings: sender)
    monkeypatch.setattr(usage, "_dispatch", lambda fn: fn())
    usage._last_alert.clear()
    return sender


async def _tiny_storage_plan(owner_engine, tenant_id) -> None:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s, s.begin():
        await s.execute(delete(Subscription).where(Subscription.tenant_id == tenant_id))
        plan = Plan(
            id=uuid4(),
            code=f"tiny_{uuid4().hex[:8]}",
            name="Tiny",
            max_storage_mb=1,
            max_orders_per_month=1,
        )
        s.add(plan)
        await s.flush()
        s.add(Subscription(tenant_id=tenant_id, plan_id=plan.id, status="active"))


async def test_contact_upload_over_storage_cap_is_accepted_and_admin_mailed(
    owner_engine, demo_tenant, captured_alerts
) -> None:
    from app.models.tenant import Tenant
    from app.platform.usage import PlanLimitExceeded
    from app.services.attachment_service import create_attachment_row

    people = await _seed_people(owner_engine, demo_tenant.id)
    await _tiny_storage_plan(owner_engine, demo_tenant.id)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s, s.begin():
        tenant = (await s.execute(select(Tenant).where(Tenant.id == demo_tenant.id))).scalar_one()
        order = Order(
            id=uuid4(),
            tenant_id=tenant.id,
            customer_id=people["customer"].id,
            number="2026-000001",
            title="Bracket",
        )
        s.add(order)
        await s.flush()
        big = 3 * 1024 * 1024
        att = await create_attachment_row(
            s,
            tenant=tenant,
            order=order,
            filename="drawing.pdf",
            content_type="application/pdf",
            size_bytes=big,
            max_size_bytes=50 * 1024 * 1024,
            uploaded_by_contact_id=people["contact"].id,
        )
        assert att.id is not None
        # Staff over the same cap still get the friendly 402 path.
        with pytest.raises(PlanLimitExceeded):
            await create_attachment_row(
                s,
                tenant=tenant,
                order=order,
                filename="more.pdf",
                content_type="application/pdf",
                size_bytes=big,
                max_size_bytes=50 * 1024 * 1024,
                uploaded_by_user_id=people["admin"].id,
            )

    assert [m.to for m in captured_alerts.outbox] == ["admin@4mex.cz"]
    assert "4MEX" in captured_alerts.outbox[0].subject


async def test_contact_order_over_cap_is_accepted(
    owner_engine, demo_tenant, captured_alerts
) -> None:
    from app.services.order_service import ActorRef, create_order

    people = await _seed_people(owner_engine, demo_tenant.id)
    await _tiny_storage_plan(owner_engine, demo_tenant.id)  # max 1 order / month
    actor = ActorRef(type="contact", id=people["contact"].id, customer_id=people["customer"].id)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s, s.begin():
        for title in ("one", "two", "three"):
            await create_order(
                s,
                tenant_id=demo_tenant.id,
                actor=actor,
                customer_id=people["customer"].id,
                title=title,
            )
    # Throttled: one mail per metric per day, not one per order.
    assert len(captured_alerts.outbox) == 1


# ----------------------------------------------- Codex-10 serialised count


async def test_limit_check_is_serialised_per_tenant(owner_engine, demo_tenant) -> None:
    """Two transactions cannot both count the same last free slot."""
    from app.platform.usage import ensure_within_limit

    await _seed_people(owner_engine, demo_tenant.id)
    await _tiny_storage_plan(owner_engine, demo_tenant.id)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)

    async def _second() -> None:
        async with sm() as second, second.begin():
            await ensure_within_limit(second, tenant_id=demo_tenant.id, metric="orders")

    async with sm() as first, first.begin():
        await ensure_within_limit(first, tenant_id=demo_tenant.id, metric="orders")
        # A second transaction must wait for the first one's commit.
        task = asyncio.create_task(_second())
        await asyncio.sleep(0.5)
        assert not task.done(), "second count ran while the first held the slot"
    # First transaction ended → the second proceeds.
    await asyncio.wait_for(task, timeout=5)


# ------------------------------------------------- LOGIC-8 / LOGIC-20 / LOGIC-9


async def _cap_users(owner_engine, tenant_id, max_users: int) -> None:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s, s.begin():
        await s.execute(delete(Subscription).where(Subscription.tenant_id == tenant_id))
        plan = Plan(id=uuid4(), code=f"cap_{uuid4().hex[:8]}", name="Cap", max_users=max_users)
        s.add(plan)
        await s.flush()
        s.add(Subscription(tenant_id=tenant_id, plan_id=plan.id, status="active"))


async def test_reactivating_a_user_respects_the_seat_cap(
    tenant_client, owner_engine, demo_tenant
) -> None:
    """disable → invite replacement → reactivate used to bypass the cap."""
    people = await _seed_people(owner_engine, demo_tenant.id)  # admin + operator
    await _cap_users(owner_engine, demo_tenant.id, max_users=2)
    await _login(tenant_client, "admin@4mex.cz", "adminpass")

    op_id = people["operator"].id
    resp = await tenant_client.post(f"/app/admin/users/{op_id}/disable", follow_redirects=False)
    assert resp.status_code == 303
    resp = await tenant_client.post(
        "/app/admin/users/invite",
        data={"email": "new@4mex.cz", "full_name": "New", "role": "tenant_staff"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    resp = await tenant_client.post(f"/app/admin/users/{op_id}/reactivate", follow_redirects=False)
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    async with owner_engine.connect() as conn:
        active = (
            await conn.execute(text("SELECT is_active FROM users WHERE id = :id"), {"id": op_id})
        ).scalar_one()
    assert active is False


async def test_support_user_is_not_a_paid_seat(owner_engine, demo_tenant) -> None:
    from app.platform.models import Identity, TenantMembership
    from app.platform.usage import snapshot_tenant_usage

    await _seed_people(owner_engine, demo_tenant.id)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s, s.begin():
        support = User(
            id=uuid4(),
            tenant_id=demo_tenant.id,
            email="root@platform.local",
            full_name="root",
            role=UserRole.TENANT_ADMIN,
        )
        ident = Identity(
            id=uuid4(),
            email="root@platform.local",
            full_name="root",
            password_hash=hash_password("x" * 12),
            is_platform_admin=True,
        )
        s.add_all([support, ident])
        await s.flush()
        s.add(
            TenantMembership(
                id=uuid4(),
                identity_id=ident.id,
                tenant_id=demo_tenant.id,
                user_id=support.id,
                access_type="support",
            )
        )
    async with sm() as s:
        usage = await snapshot_tenant_usage(s, demo_tenant.id)
    assert usage.users == 2  # admin + operator, not the operator's support grant


async def test_last_admin_guard_ignores_never_accepted_admins(owner_engine, demo_tenant) -> None:
    """LOGIC-9: a pending co-admin (or a password-less support user) is
    not an admin who could take over — the guard must not count it."""
    from app.routers.tenant_admin import _other_active_admins_exist

    people = await _seed_people(owner_engine, demo_tenant.id)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s, s.begin():
        s.add(
            User(
                id=uuid4(),
                tenant_id=demo_tenant.id,
                email="pending@4mex.cz",
                full_name="Pending",
                role=UserRole.TENANT_ADMIN,
                password_hash=None,
            )
        )
    async with sm() as s:
        assert not await _other_active_admins_exist(s, exclude_user_id=people["admin"].id)
        s.add(
            User(
                id=uuid4(),
                tenant_id=demo_tenant.id,
                email="coadmin@4mex.cz",
                full_name="Co-admin",
                role=UserRole.TENANT_ADMIN,
                password_hash=hash_password("coadminpass"),
            )
        )
        await s.flush()
        assert await _other_active_admins_exist(s, exclude_user_id=people["admin"].id)
        await s.rollback()


# ---------------------------------------------------- LOGIC-20 / Codex-11 mail


async def test_support_user_never_receives_tenant_order_mail(
    owner_engine, demo_tenant, settings
) -> None:
    from app.platform.models import Identity, TenantMembership
    from app.services.notification_service import build_order_submitted

    people = await _seed_people(owner_engine, demo_tenant.id)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s, s.begin():
        # Every real staff member opted out of new-order mail …
        await s.execute(
            text(
                "UPDATE users SET notification_prefs = "
                '\'{"events": {"order_submitted": false}}\'::jsonb WHERE tenant_id = :t'
            ),
            {"t": demo_tenant.id},
        )
        # … and the operator holds a support grant.
        support = User(
            id=uuid4(),
            tenant_id=demo_tenant.id,
            email="root@platform.local",
            full_name="root",
            role=UserRole.TENANT_ADMIN,
        )
        ident = Identity(
            id=uuid4(),
            email="root@platform.local",
            full_name="root",
            password_hash=hash_password("x" * 12),
            is_platform_admin=True,
        )
        s.add_all([support, ident])
        await s.flush()
        s.add(
            TenantMembership(
                id=uuid4(),
                identity_id=ident.id,
                tenant_id=demo_tenant.id,
                user_id=support.id,
                access_type="support",
            )
        )
        order = Order(
            id=uuid4(),
            tenant_id=demo_tenant.id,
            customer_id=people["customer"].id,
            number="2026-000009",
            title="X",
        )
        s.add(order)
    async with sm() as s:
        await s.execute(
            text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(demo_tenant.id)}
        )
        order = (await s.execute(select(Order).where(Order.id == order.id))).scalar_one()
        payloads = await build_order_submitted(
            s, tenant=demo_tenant, order=order, base_url="http://x", settings=settings
        )
    assert "root@platform.local" not in {p.recipient.email for p in payloads}


async def test_billing_fallback_skips_a_disabled_owner_address(
    owner_engine, demo_tenant, settings
) -> None:
    """Codex-11: billing_email belonging to a disabled former owner is not
    an authorised reader of order content."""
    from app.services.notification_service import build_order_submitted

    people = await _seed_people(owner_engine, demo_tenant.id)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as s, s.begin():
        await s.execute(
            text("UPDATE users SET is_active = false WHERE tenant_id = :t"), {"t": demo_tenant.id}
        )
        s.add(
            User(
                id=uuid4(),
                tenant_id=demo_tenant.id,
                email=demo_tenant.billing_email,
                full_name="Former owner",
                role=UserRole.TENANT_ADMIN,
                password_hash=hash_password("formerpass"),
                is_active=False,
            )
        )
        order = Order(
            id=uuid4(),
            tenant_id=demo_tenant.id,
            customer_id=people["customer"].id,
            number="2026-000010",
            title="X",
        )
        s.add(order)
    async with sm() as s:
        order = (await s.execute(select(Order).where(Order.id == order.id))).scalar_one()
        payloads = await build_order_submitted(
            s, tenant=demo_tenant, order=order, base_url="http://x", settings=settings
        )
    assert payloads == []


def test_fallback_is_only_for_new_orders() -> None:
    from app.services.notification_service import _FALLBACK_EVENTS, NotificationEvent

    assert frozenset({NotificationEvent.ORDER_SUBMITTED}) == _FALLBACK_EVENTS
