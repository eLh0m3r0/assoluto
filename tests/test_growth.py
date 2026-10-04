"""Growth: activation funnel, honest MRR, signup attribution, viral footer.

Covers BIZ-09 (bots out of the funnel and MRR, disposable signups),
BIZ-16 (activation funnel in platform admin) and MKT-9 (the "Powered by
Assoluto" footer + ``?ref=portal`` attribution on signup).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import ASGITransport
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.email.sender import CaptureSender, render_email
from app.main import create_app
from app.models.customer import Customer, CustomerContact
from app.models.enums import UserRole
from app.models.order import Order
from app.models.tenant import Tenant
from app.models.user import User
from app.platform.models import Identity, TenantMembership
from app.security.passwords import hash_password
from app.urls import powered_by_url
from tests.conftest import CsrfAwareClient

# ------------------------------------------------------------ powered-by URL


def _settings(**kw):
    base = {"powered_by_url": "", "platform_cookie_domain": ""}
    base.update(kw)
    return SimpleNamespace(**base)


def _tenant(slug="acme", settings=None):
    return SimpleNamespace(slug=slug, settings=settings or {})


def test_powered_by_hidden_on_self_hosted() -> None:
    assert powered_by_url(_settings(), _tenant()) == ""


def test_powered_by_derived_from_cookie_domain() -> None:
    url = powered_by_url(_settings(platform_cookie_domain=".assoluto.eu"), _tenant("4mex"))
    assert url == "https://assoluto.eu/?ref=portal&t=4mex"


def test_powered_by_explicit_url_and_off_switch() -> None:
    assert (
        powered_by_url(_settings(powered_by_url="https://example.test"), _tenant("x"))
        == "https://example.test/?ref=portal&t=x"
    )
    assert (
        powered_by_url(_settings(powered_by_url="off", platform_cookie_domain=".a.eu"), _tenant())
        == ""
    )


def test_powered_by_white_label_tenant_opts_out() -> None:
    tenant = _tenant(settings={"hide_powered_by": True})
    assert powered_by_url(_settings(platform_cookie_domain=".assoluto.eu"), tenant) == ""
    assert powered_by_url(_settings(platform_cookie_domain=".assoluto.eu"), None) == ""


def test_contact_email_templates_render_footer_only_when_url_set() -> None:
    ctx = {
        "tenant_name": "4MEX",
        "order_number": "2026-000001",
        "order_title": "Bracket",
        "order_url": "https://4mex.example/app/orders/1",
        "recipient_name": "Jan",
        "status_label": "Ready",
        "author_name": "Staff",
        "body_excerpt": "Hi",
        "customer_name": "ACME",
        "contact_name": "Jan",
        "invite_url": "https://4mex.example/invite/accept?token=x",
    }
    link = "https://assoluto.eu/?ref=portal&t=4mex"
    for template in ("order_status_changed", "order_comment", "order_created", "invitation"):
        with_footer = render_email(template, {**ctx, "powered_by_url": link}, locale="en")
        assert link.replace("&", "&amp;") in with_footer.html, template
        assert link in with_footer.text, template
        assert "a customer portal for manufacturers" in with_footer.text
        without = render_email(template, {**ctx, "powered_by_url": ""}, locale="en")
        assert "ref=portal" not in without.html
        assert "ref=portal" not in without.text


@pytest.mark.postgres
async def test_contact_audience_carries_footer_staff_does_not(
    owner_engine, demo_tenant, settings
) -> None:
    from app.models.enums import OrderStatus
    from app.services.notification_service import build_order_comment

    settings.platform_cookie_domain = ".assoluto.test"
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        customer = Customer(id=uuid4(), tenant_id=demo_tenant.id, name="ACME")
        session.add(customer)
        await session.flush()
        session.add(
            User(
                id=uuid4(),
                tenant_id=demo_tenant.id,
                email="staff@4mex.cz",
                full_name="Staff",
                role=UserRole.TENANT_ADMIN,
                password_hash=hash_password("x" * 10),
            )
        )
        session.add(
            CustomerContact(
                id=uuid4(),
                tenant_id=demo_tenant.id,
                customer_id=customer.id,
                email="buyer@acme.cz",
                full_name="Buyer",
                password_hash=hash_password("x" * 10),
                invited_at=datetime.now(UTC),
                accepted_at=datetime.now(UTC),
            )
        )
        order = Order(
            id=uuid4(),
            tenant_id=demo_tenant.id,
            customer_id=customer.id,
            number="2026-000001",
            title="Bracket",
            status=OrderStatus.SUBMITTED,
        )
        session.add(order)

    async with sm() as session:
        order = (await session.execute(select(Order))).scalar_one()
        to_contacts = await build_order_comment(
            session,
            tenant=demo_tenant,
            order=order,
            author_name="Staff",
            author_email="staff@4mex.cz",
            author_is_staff=True,
            body="Ready on Friday",
            base_url="https://4mex.assoluto.test",
            settings=settings,
        )
        to_staff = await build_order_comment(
            session,
            tenant=demo_tenant,
            order=order,
            author_name="Buyer",
            author_email="buyer@acme.cz",
            author_is_staff=False,
            body="Thanks",
            base_url="https://4mex.assoluto.test",
            settings=settings,
        )
    assert [p.recipient.email for p in to_contacts] == ["buyer@acme.cz"]
    assert to_contacts[0].context()["powered_by_url"] == (
        "https://assoluto.test/?ref=portal&t=4mex"
    )
    assert [p.recipient.email for p in to_staff] == ["staff@4mex.cz"]
    assert to_staff[0].context()["powered_by_url"] == ""


# --------------------------------------------------------- platform fixtures


@pytest.fixture
async def platform(settings, wipe_db, owner_engine) -> AsyncIterator[CsrfAwareClient]:
    settings.feature_platform = True
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        session.add(
            Identity(
                id=uuid4(),
                email="root@platform.local",
                full_name="Root",
                password_hash=hash_password("rootpass"),
                is_platform_admin=True,
                email_verified_at=datetime.now(UTC),
            )
        )
    from app.platform.deps import reset_platform_engine

    reset_platform_engine()
    app = create_app(settings)
    app.state.email_sender = CaptureSender()
    transport = ASGITransport(app=app)
    async with CsrfAwareClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    reset_platform_engine()


async def _login_root(client) -> None:
    resp = await client.post(
        "/platform/login",
        data={"email": "root@platform.local", "password": "rootpass"},
        follow_redirects=False,
    )
    assert resp.status_code == 303


async def _signup(client, slug: str, email: str, **extra) -> None:
    resp = await client.post(
        "/platform/signup",
        data={
            "company_name": f"Firma {slug}",
            "slug": slug,
            "owner_email": email,
            "owner_full_name": "Owner",
            "password": "correct-horse-battery-staple",
            "terms_accepted": "1",
            **extra,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    client.cookies.pop("sme_portal_platform", None)


# ------------------------------------------------------- signup attribution


@pytest.mark.postgres
async def test_signup_form_carries_ref_from_query(platform) -> None:
    resp = await platform.get("/platform/signup?ref=portal&t=4mex")
    assert 'name="ref" value="portal"' in resp.text
    assert 'name="ref_t" value="4mex"' in resp.text


@pytest.mark.postgres
async def test_signup_form_reads_ref_from_same_origin_referer(platform) -> None:
    resp = await platform.get(
        "/platform/signup", headers={"referer": "http://testserver/?ref=portal&t=kovarna"}
    )
    assert 'name="ref_t" value="kovarna"' in resp.text
    # A foreign site cannot plant attribution.
    resp = await platform.get(
        "/platform/signup", headers={"referer": "https://evil.test/?ref=portal&t=x"}
    )
    assert 'name="ref"' not in resp.text


@pytest.mark.postgres
async def test_signup_records_ref_on_tenant(platform, owner_engine) -> None:
    await _signup(platform, "novy-odberatel", "owner@novy-odberatel.cz", ref="portal", ref_t="4mex")
    await _signup(platform, "bez-refu", "owner@bez-refu.cz", ref="spam", ref_t="x")
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        tenants = {t.slug: t for t in (await session.execute(select(Tenant))).scalars().all()}
    assert tenants["novy-odberatel"].settings["signup_ref"] == {"ref": "portal", "t": "4mex"}
    assert "signup_ref" not in (tenants["bez-refu"].settings or {})


@pytest.mark.postgres
async def test_signup_rejects_disposable_email(platform, owner_engine) -> None:
    resp = await platform.post(
        "/platform/signup",
        data={
            "company_name": "Throwaway",
            "slug": "throwaway",
            "owner_email": "someone@mailinator.com",
            "owner_full_name": "X",
            "password": "correct-horse-battery-staple",
            "terms_accepted": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "permanent work email" in resp.text or "trvalý pracovní e-mail" in resp.text
    async with owner_engine.connect() as conn:
        n = (await conn.execute(text("SELECT count(*) FROM tenants"))).scalar_one()
    assert n == 0


# ---------------------------------------------------------- funnel + MRR


async def _activate(owner_engine, slug: str, *, login: bool, order: bool) -> None:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        tenant = (await session.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one()
        customer = Customer(id=uuid4(), tenant_id=tenant.id, name="Odběratel")
        session.add(customer)
        await session.flush()
        contact = CustomerContact(
            id=uuid4(),
            tenant_id=tenant.id,
            customer_id=customer.id,
            email=f"buyer@{slug}.test",
            full_name="Buyer",
            invited_at=datetime.now(UTC),
            last_login_at=datetime.now(UTC) if login else None,
        )
        session.add(contact)
        await session.flush()
        if order:
            session.add(
                Order(
                    id=uuid4(),
                    tenant_id=tenant.id,
                    customer_id=customer.id,
                    number="2026-000001",
                    title="First",
                    created_by_contact_id=contact.id,
                )
            )


async def _verify(owner_engine, email: str) -> None:
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE platform_identities SET email_verified_at = now() WHERE email = :e"),
            {"e": email},
        )


@pytest.mark.postgres
async def test_weekly_funnel_excludes_unverified(platform, owner_engine) -> None:
    from app.platform.activation import funnel_totals, weekly_funnel

    await _signup(platform, "bot-shell", "zxqwvbnmlkjh@gmail.com")  # never verifies
    await _signup(platform, "invited-only", "a@invited-only.cz")
    await _signup(platform, "activated", "a@activated-firma.cz")
    await _verify(owner_engine, "a@invited-only.cz")
    await _verify(owner_engine, "a@activated-firma.cz")
    await _activate(owner_engine, "invited-only", login=False, order=False)
    await _activate(owner_engine, "activated", login=True, order=True)
    # An unverified signup that somehow has activity still never counts.
    await _activate(owner_engine, "bot-shell", login=True, order=True)

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        weeks = await weekly_funnel(session, weeks=4)
    totals = funnel_totals(weeks)
    assert len(weeks) == 4
    assert totals.unverified == 1
    assert totals.verified == 2
    assert totals.tenant_created == 2
    assert totals.customer_invited == 2
    assert totals.contact_logged_in == 1
    assert totals.contact_ordered == 1


@pytest.mark.postgres
async def test_funnel_page_renders_for_platform_admin(platform, owner_engine) -> None:
    await _signup(platform, "funnel-a", "a@funnel-firma.cz", ref="portal", ref_t="4mex")
    await _verify(owner_engine, "a@funnel-firma.cz")
    await _login_root(platform)
    resp = await platform.get("/platform/admin/funnel")
    assert resp.status_code == 200
    assert "funnel-a" in resp.text
    assert "/platform/admin/funnel" in resp.text  # nav entry


@pytest.mark.postgres
async def test_funnel_page_requires_platform_admin(platform) -> None:
    resp = await platform.get("/platform/admin/funnel", follow_redirects=False)
    assert resp.status_code in (303, 401)


@pytest.mark.postgres
async def test_mrr_counts_only_paying_subscriptions(platform, owner_engine) -> None:
    from app.platform.billing.models import Plan, Subscription

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        plan = Plan(id=uuid4(), code=f"t-{uuid4().hex[:6]}", name="T", monthly_price_cents=777700)
        session.add(plan)
        for slug, status in (("trial-co", "trialing"), ("demo-co", "demo")):
            tenant = Tenant(
                id=uuid4(),
                slug=slug,
                name=slug,
                billing_email="b@x.cz",
                storage_prefix=f"tenants/{slug}/",
            )
            session.add(tenant)
            await session.flush()
            session.add(
                Subscription(
                    id=uuid4(),
                    tenant_id=tenant.id,
                    plan_id=plan.id,
                    status=status,
                    trial_ends_at=datetime.now(UTC) + timedelta(days=10),
                )
            )
    await _login_root(platform)
    resp = await platform.get("/platform/admin/dashboard")
    assert resp.status_code == 200
    assert "7 777" not in resp.text  # trials and demo are not revenue

    async with sm() as session, session.begin():
        tenant = Tenant(
            id=uuid4(),
            slug="paying-co",
            name="P",
            billing_email="b@x.cz",
            storage_prefix="tenants/p/",
        )
        session.add(tenant)
        await session.flush()
        session.add(Subscription(id=uuid4(), tenant_id=tenant.id, plan_id=plan.id, status="active"))
    resp = await platform.get("/platform/admin/dashboard")
    assert "7 777 Kč" in resp.text


@pytest.mark.postgres
async def test_unverified_trial_not_counted_as_trial(platform, owner_engine) -> None:
    await _signup(platform, "bot-trial", "zxqwvbnmlkjh@gmail.com")
    await _login_root(platform)
    resp = await platform.get("/platform/admin/dashboard")
    assert resp.status_code == 200
    # The bot's trialing subscription exists but is not a "verified trial".
    async with owner_engine.connect() as conn:
        subs = (
            await conn.execute(
                text("SELECT count(*) FROM platform_subscriptions WHERE status = 'trialing'")
            )
        ).scalar_one()
    assert subs == 1
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        tm = (await session.execute(select(TenantMembership))).scalars().all()
    assert len(tm) == 1
    import re

    match = re.search(r"(?:with verified e-mail|s ověřeným e-mailem):\s*(\d+)", resp.text)
    assert match is not None and match.group(1) == "0"
