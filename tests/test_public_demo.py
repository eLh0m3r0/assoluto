"""Public demo without sign-up (CEO decision E3).

Covers the entry (``/demo``), the guard (no e-mail, blocked account /
invitation / settings actions, capped uploads, banner, ``noindex``,
logout that does not sign out other visitors), the marketing links, the
nightly reset job, and — just as important — that every other tenant is
completely unaffected.
"""

# Order titles come from the seed, which writes them the Czech way (en dash).
# ruff: noqa: RUF001

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import boto3
import pytest
from httpx import ASGITransport
from moto import mock_aws
from PIL import Image
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.demo.seed import DEMO_CONTACT_EMAIL, DEMO_STAFF_EMAIL, SeedResult, seed_demo
from app.main import create_app
from app.models.tenant import Tenant
from tests.conftest import CsrfAwareClient

pytestmark = pytest.mark.postgres

DEMO = "ukazka-t"
OTHER = "dilna-t"
PASSWORD = "Demo-heslo-1"


def _t_any(msgid: str) -> tuple[str, ...]:
    """The text in any shipped locale — pages render in the visitor's language."""
    from app.i18n import gettext

    return tuple({gettext(loc, msgid) for loc in ("en", "cs", "de")})


@pytest.fixture(autouse=True)
def _s3_env(monkeypatch):  # type: ignore[misc]
    monkeypatch.setenv("S3_ENDPOINT_URL", "")
    monkeypatch.setenv("S3_PUBLIC_ENDPOINT_URL", "")
    monkeypatch.setenv("S3_ACCESS_KEY", "test")
    monkeypatch.setenv("S3_SECRET_KEY", "test")
    monkeypatch.setenv("S3_BUCKET", "portal-demo-test")
    monkeypatch.setenv("S3_REGION", "eu-central-1")


@pytest.fixture
def mock_s3(settings):  # type: ignore[misc]
    from app.storage import s3 as s3_mod

    with mock_aws():
        s3_mod.get_s3_client.cache_clear()
        s3_mod.get_public_s3_client.cache_clear()
        boto3.client("s3", region_name="eu-central-1").create_bucket(
            Bucket=settings.s3_bucket,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )
        yield
        s3_mod.get_s3_client.cache_clear()
        s3_mod.get_public_s3_client.cache_clear()


def _keys(prefix: str = "") -> list[str]:
    from app.storage import s3 as s3_storage

    return sorted(o["key"] for o in s3_storage.list_objects(prefix))


@pytest.fixture
async def demo(settings, owner_engine, wipe_db) -> SeedResult:
    """The public demo tenant (no files) with PUBLIC_DEMO_TENANT pointing at it."""
    result = await seed_demo(slug=DEMO, password=PASSWORD, engine=owner_engine, files=False)
    settings.public_demo_tenant = DEMO
    return result


async def _client(settings, slug: str) -> CsrfAwareClient:
    app = create_app(settings)
    return CsrfAwareClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"X-Tenant-Slug": slug},
    )


@pytest.fixture
async def demo_client(settings, demo) -> AsyncIterator[CsrfAwareClient]:
    async with await _client(settings, DEMO) as client:
        yield client


async def _enter(client: CsrfAwareClient, role: str) -> None:
    resp = await client.post("/demo/enter", data={"role": role}, follow_redirects=False)
    assert resp.status_code == 303, resp.text[:300]
    # The supplier lands on the dashboard, the customer on its open quote
    # (P2-14, see tests/test_demo_ui.py).
    if role == "staff":
        assert resp.headers["location"] == "/app"
    else:
        assert resp.headers["location"].startswith("/app/orders/")


def _sender(client: CsrfAwareClient):
    return client._transport.app.state.email_sender  # type: ignore[attr-defined]


async def _order_id(owner_engine, slug: str, title: str) -> UUID:
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT o.id FROM orders o JOIN tenants t ON t.id = o.tenant_id "
                    "WHERE t.slug = :s AND o.title = :title"
                ),
                {"s": slug, "title": title},
            )
        ).scalar_one()


def _png(size: int = 32) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (size, size), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


# ------------------------------------------------------------------ entry


async def test_demo_page_offers_both_roles_and_is_noindex(demo_client) -> None:
    resp = await demo_client.get("/demo")
    assert resp.status_code == 200
    assert resp.headers["x-robots-tag"] == "noindex, nofollow"
    assert "no-store" in resp.headers["cache-control"]
    assert 'name="role" value="staff"' in resp.text
    assert 'name="role" value="customer"' in resp.text
    assert 'action="/demo/enter"' in resp.text
    # Banner + "Create your own portal" pointing at the apex signup.
    assert any(
        t in resp.text
        for t in _t_any(
            "This is a public demo with fictional data. Anything you change is reset every night."
        )
    )
    assert "/platform/signup" in resp.text


async def test_entry_as_supplier_gives_a_staff_session(demo_client) -> None:
    await _enter(demo_client, "staff")
    resp = await demo_client.get("/app/orders")
    assert resp.status_code == 200
    assert resp.headers["x-robots-tag"] == "noindex, nofollow"
    assert any(
        t in resp.text
        for t in _t_any(
            "This is a public demo with fictional data. Anything you change is reset every night."
        )
    )
    # Staff see every client's orders.
    assert "Konzole KM-120" in resp.text
    assert "Příruby P30" in resp.text
    assert (await demo_client.get("/app/customers")).status_code == 200


async def test_entry_as_customer_gives_a_contact_session(demo_client) -> None:
    await _enter(demo_client, "customer")
    resp = await demo_client.get("/app/orders")
    assert resp.status_code == 200
    assert "Konzole KM-120" in resp.text
    assert "Příruby P30" not in resp.text  # another client's order
    # Staff-only area stays closed to the customer persona.
    assert (await demo_client.get("/app/customers", follow_redirects=False)).status_code in (
        303,
        403,
    )


async def test_chooser_offers_continue_only_for_a_live_session(demo_client, owner_engine) -> None:
    page = (await demo_client.get("/demo")).text
    assert not any(t in page for t in _t_any("Continue where you left off"))
    await _enter(demo_client, "staff")
    page = (await demo_client.get("/demo")).text
    assert any(t in page for t in _t_any("Continue where you left off"))
    # The nightly reset re-creates the personas: the old cookie is dead.
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET session_version = session_version + 1 WHERE email = :e"),
            {"e": DEMO_STAFF_EMAIL},
        )
    resp = await demo_client.get("/demo")
    assert not any(t in resp.text for t in _t_any("Continue where you left off"))
    assert "sme_portal_session" in resp.headers.get("set-cookie", "")  # cleared


async def test_entry_rejects_unknown_role(demo_client) -> None:
    resp = await demo_client.post("/demo/enter", data={"role": "admin"}, follow_redirects=False)
    assert resp.status_code == 400


async def test_entry_explains_a_missing_persona(demo_client, owner_engine) -> None:
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET is_active = false WHERE email = :e"), {"e": DEMO_STAFF_EMAIL}
        )
    resp = await demo_client.post("/demo/enter", data={"role": "staff"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/demo?error=")


async def test_login_and_index_lead_to_the_chooser(demo_client) -> None:
    for path in ("/", "/auth/login", "/auth/login?next=/app/orders"):
        resp = await demo_client.get(path, follow_redirects=False)
        assert resp.status_code == 303, path
        assert resp.headers["location"] == "/demo"


async def test_logout_does_not_sign_out_other_visitors(settings, demo) -> None:
    async with (
        await _client(settings, DEMO) as alice,
        await _client(settings, DEMO) as bob,
    ):
        await _enter(alice, "staff")
        await _enter(bob, "staff")
        resp = await alice.post("/auth/logout", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/demo"
        assert "sme_portal_session" in resp.headers.get("set-cookie", "")
        # Bob shares the persona; a normal logout would have bumped its
        # session_version and thrown him out too.
        assert (await bob.get("/app/orders")).status_code == 200


async def test_demo_routes_404_on_any_other_tenant(settings, demo, owner_engine) -> None:
    await seed_demo(slug=OTHER, password=PASSWORD, engine=owner_engine, files=False)
    async with await _client(settings, OTHER) as client:
        assert (await client.get("/demo")).status_code == 404
        resp = await client.post("/demo/enter", data={"role": "staff"}, follow_redirects=False)
        assert resp.status_code == 404


async def test_demo_is_off_when_the_setting_is_empty(settings, demo) -> None:
    settings.public_demo_tenant = ""
    async with await _client(settings, DEMO) as client:
        resp = await client.get("/demo")
        assert resp.status_code == 404
        assert "x-robots-tag" not in resp.headers
        login = await client.get("/auth/login")
        assert login.status_code == 200  # the normal sign-in form


async def test_only_a_seeded_tenant_can_be_public(settings, owner_engine, wipe_db) -> None:
    """PUBLIC_DEMO_TENANT pointing at a real tenant (no seed marker) is refused."""
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        session.add(
            Tenant(
                id=uuid4(),
                slug="real-shop",
                name="Real",
                billing_email="b@real.example.com",
                storage_prefix="tenants/real-shop/",
            )
        )
    settings.public_demo_tenant = "real-shop"
    async with await _client(settings, "real-shop") as client:
        resp = await client.get("/demo")
        assert resp.status_code == 404
        assert "x-robots-tag" not in resp.headers
        assert (await client.get("/auth/login")).status_code == 200


async def test_entry_is_rate_limited_per_ip(demo_client) -> None:
    from app.security.rate_limit import limiter

    try:
        limiter.enabled = True
        limiter.reset()
        for _ in range(20):
            resp = await demo_client.post(
                "/demo/enter", data={"role": "customer"}, follow_redirects=False
            )
            assert resp.status_code == 303
        resp = await demo_client.post(
            "/demo/enter", data={"role": "customer"}, follow_redirects=False
        )
        assert resp.status_code == 429
    finally:
        limiter.enabled = False
        limiter.reset()


# ------------------------------------------------------------------ guard

BLOCKED_AS_STAFF = [
    ("POST", "/app/admin/profile/password", "account"),
    ("POST", "/app/admin/profile", "account"),
    ("POST", "/app/admin/profile/delete", "account"),
    ("GET", "/app/admin/profile/export", "export"),
    ("GET", "/app/admin/export", "export"),
    ("POST", "/app/admin/users/invite", "invite"),
    ("POST", "/app/admin/tenant-settings", "settings"),
    ("POST", "/auth/password-reset", "account"),
    ("GET", "/auth/password-reset", "account"),
    ("POST", "/platform/billing/checkout/pro", "other"),
]


@pytest.mark.parametrize(("method", "path", "kind"), BLOCKED_AS_STAFF)
async def test_blocked_actions_redirect_with_a_friendly_flash(
    demo_client, method: str, path: str, kind: str
) -> None:
    await _enter(demo_client, "staff")
    resp = await demo_client.request(
        method,
        path,
        data={"current_password": PASSWORD, "new_password": "x" * 12} if method == "POST" else None,
        headers={"Referer": "http://testserver/app/orders?notice=old"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, (path, resp.status_code)
    location = resp.headers["location"]
    assert f"demo_blocked={kind}" in location
    if path != "/auth/password-reset":
        assert location.startswith("/app/orders?")
        assert "notice=old" not in location  # stale flash dropped
    page = await demo_client.get(location)
    assert page.status_code == 200
    assert 'role="alert"' in page.text


async def test_blocked_actions_change_nothing(demo_client, owner_engine, demo) -> None:
    await _enter(demo_client, "staff")
    async with owner_engine.connect() as conn:
        tid = demo.tenant_id
        user_id, pw_before, sv_before = (
            await conn.execute(
                text("SELECT id, password_hash, session_version FROM users WHERE email = :e"),
                {"e": DEMO_STAFF_EMAIL},
            )
        ).one()
        contact_id, customer_id = (
            await conn.execute(
                text("SELECT id, customer_id FROM customer_contacts WHERE email = :e"),
                {"e": DEMO_CONTACT_EMAIL},
            )
        ).one()
        planner_id = (
            await conn.execute(
                text("SELECT id FROM users WHERE tenant_id = :t AND email LIKE 'planovani@%'"),
                {"t": tid},
            )
        ).scalar_one()

    attempts = [
        ("/app/admin/profile/password", {"current_password": PASSWORD, "new_password": "N" * 14,
                                         "new_password_confirm": "N" * 14}),
        ("/app/admin/profile", {"full_name": "Hacked", "email": "evil@attacker.test"}),
        (f"/app/admin/users/{planner_id}/disable", {}),
        (f"/app/admin/users/{planner_id}/edit", {"full_name": "X", "role": "tenant_admin"}),
        ("/app/admin/users/invite", {"email": "victim@attacker.test", "full_name": "V"}),
        (f"/app/customers/{customer_id}/archive", {}),
        (f"/app/customers/{customer_id}/contacts", {"email": "v@attacker.test", "full_name": "V"}),
        (f"/app/customers/{customer_id}/contacts/{contact_id}/disable", {}),
        (f"/app/customers/{customer_id}/contacts/{contact_id}/resend-invite", {}),
    ]  # fmt: skip
    for path, data in attempts:
        resp = await demo_client.post(path, data=data, follow_redirects=False)
        assert resp.status_code == 303, path
        assert "demo_blocked=" in resp.headers["location"], path

    async with owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT password_hash, session_version, full_name, email, is_active "
                    "FROM users WHERE id = :id"
                ),
                {"id": user_id},
            )
        ).one()
        assert (row.password_hash, row.session_version) == (pw_before, sv_before)
        assert row.email == DEMO_STAFF_EMAIL and row.full_name != "Hacked"
        assert (
            await conn.execute(
                text("SELECT is_active FROM users WHERE id = :id"), {"id": planner_id}
            )
        ).scalar_one() is True
        assert (
            await conn.execute(
                text("SELECT is_active FROM customers WHERE id = :id"), {"id": customer_id}
            )
        ).scalar_one() is True
        assert (
            await conn.execute(
                text("SELECT is_active FROM customer_contacts WHERE id = :id"), {"id": contact_id}
            )
        ).scalar_one() is True
        strangers = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM (SELECT email FROM users WHERE tenant_id = :t "
                    "UNION ALL SELECT email FROM customer_contacts WHERE tenant_id = :t) x "
                    "WHERE email LIKE '%attacker.test'"
                ),
                {"t": tid},
            )
        ).scalar_one()
        assert strangers == 0


async def test_customer_team_invite_is_blocked(demo_client) -> None:
    await _enter(demo_client, "customer")
    resp = await demo_client.post(
        "/app/me/team/invite",
        data={"email": "v@attacker.test", "full_name": "V"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "demo_blocked=invite" in resp.headers["location"]
    resp = await demo_client.post(
        "/app/me/profile/password",
        data={"current_password": PASSWORD, "new_password": "N" * 14},
        follow_redirects=False,
    )
    assert "demo_blocked=account" in resp.headers["location"]


async def test_the_point_of_the_demo_still_works(demo_client, owner_engine) -> None:
    """Orders, comments, status changes and downloads are allowed."""
    await _enter(demo_client, "staff")
    order_id = await _order_id(owner_engine, DEMO, "Rám stojanu – prototyp")
    resp = await demo_client.post(
        f"/app/orders/{order_id}/comments", data={"body": "Nacenime do zítřka."},
        follow_redirects=False,
    )  # fmt: skip
    assert resp.status_code == 303 and "demo_blocked" not in resp.headers["location"]
    resp = await demo_client.post(
        f"/app/orders/{order_id}/transitions/quoted", follow_redirects=False
    )
    assert resp.status_code == 303 and "demo_blocked" not in resp.headers["location"]
    assert (await demo_client.get(f"/app/orders/{order_id}.pdf")).status_code == 200
    assert (await demo_client.get("/app/orders.csv")).status_code == 200
    assert (await demo_client.get("/app/admin/exports/pohoda.xml")).status_code == 200
    # Notification preferences: allowed (harmless, no mail leaves the demo).
    resp = await demo_client.post(
        "/app/admin/profile/notifications", data={"scope": "all"}, follow_redirects=False
    )
    assert "demo_blocked" not in resp.headers.get("location", "")
    resp = await demo_client.post(
        "/app/orders", data={"title": "Zkušební zakázka návštěvníka",
                             "customer_id": await _customer_id(owner_engine)},
        follow_redirects=False,
    )  # fmt: skip
    assert resp.status_code == 303 and "/app/orders/" in resp.headers["location"]


async def _customer_id(owner_engine) -> str:
    async with owner_engine.connect() as conn:
        return str(
            (
                await conn.execute(
                    text(
                        "SELECT c.id FROM customers c JOIN tenants t ON t.id = c.tenant_id "
                        "WHERE t.slug = :s ORDER BY c.name LIMIT 1"
                    ),
                    {"s": DEMO},
                )
            ).scalar_one()
        )


# ------------------------------------------------------------------- mail


async def test_no_mail_leaves_the_demo(demo_client, owner_engine, demo) -> None:
    sender = _sender(demo_client)
    await _enter(demo_client, "staff")
    order_id = await _order_id(owner_engine, DEMO, "Konzole KM-120 – série 200 ks")
    resp = await demo_client.post(
        f"/app/orders/{order_id}/comments", data={"body": "Hotovo ve čtvrtek."},
        follow_redirects=False,
    )  # fmt: skip
    assert resp.status_code == 303
    resp = await demo_client.post(
        f"/app/orders/{order_id}/transitions/ready", follow_redirects=False
    )
    assert resp.status_code == 303
    # As the customer: submit an order (staff would be told).
    demo_client.cookies.delete("sme_portal_session")
    await _enter(demo_client, "customer")
    draft = await _order_id(owner_engine, DEMO, "Výztuhy rámu – rozpracováno")
    resp = await demo_client.post(
        f"/app/orders/{draft}/transitions/submitted", follow_redirects=False
    )
    assert resp.status_code == 303

    assert sender.outbox == []
    async with owner_engine.connect() as conn:
        queued = (
            await conn.execute(
                text("SELECT count(*) FROM email_outbox WHERE tenant_id = :t"),
                {"t": demo.tenant_id},
            )
        ).scalar_one()
    assert queued == 0


async def test_another_tenant_still_gets_its_mail(settings, demo, owner_engine) -> None:
    """Same actions on a tenant that is not the public demo: mail goes out,
    no banner, no noindex, password change reaches its route."""
    other = await seed_demo(slug=OTHER, password=PASSWORD, engine=owner_engine, files=False)
    async with await _client(settings, OTHER) as client:
        resp = await client.post(
            "/auth/login",
            data={"email": other.staff_email, "password": PASSWORD},
            follow_redirects=False,
        )
        assert resp.status_code == 303 and resp.headers["location"] == "/app"
        page = await client.get("/app/orders")
        assert "x-robots-tag" not in page.headers
        assert "This is a public demo" not in page.text
        order_id = await _order_id(owner_engine, OTHER, "Konzole KM-120 – série 200 ks")
        resp = await client.post(
            f"/app/orders/{order_id}/comments", data={"body": "Hotovo ve čtvrtek."},
            follow_redirects=False,
        )  # fmt: skip
        assert resp.status_code == 303
        assert _sender(client).outbox, "a non-demo tenant must still notify its customer"
        resp = await client.post(
            "/app/admin/profile/password",
            data={"current_password": "wrong", "new_password": "N" * 14,
                  "new_password_confirm": "N" * 14},
            follow_redirects=False,
        )  # fmt: skip
        assert "demo_blocked" not in resp.headers.get("location", "")
        assert resp.status_code in (200, 400)  # the real route answered
        resp = await client.get("/auth/login", follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"] == "/app"


async def test_scheduled_mail_for_the_demo_tenant_is_dropped(settings, demo, owner_engine) -> None:
    """Periodic jobs (weekly summary, quote reminders) send outside a request:
    the payload's tenant id decides."""
    from app.email.sender import CaptureSender
    from app.services.notification_service import (
        NotificationEvent,
        OrderNotification,
        Recipient,
    )
    from app.tasks.email_tasks import send_order_notifications

    other = await seed_demo(slug=OTHER, password=PASSWORD, engine=owner_engine, files=False)
    sender = CaptureSender()

    def payload(tenant_id):
        return OrderNotification(
            event=NotificationEvent.QUOTE_REMINDER,
            recipient=Recipient(email="nakup@ukazkova.example.com", locale="cs"),
            tenant_name="X",
            order_number="2026-000001",
            order_title="T",
            order_url="http://x/app/orders/1",
            tenant_id=tenant_id,
        )

    send_order_notifications(sender, [payload(demo.tenant_id)])
    assert sender.outbox == []
    send_order_notifications(sender, [payload(other.tenant_id)])
    assert len(sender.outbox) == 1


async def test_outbox_retry_job_drops_demo_rows(settings, demo, owner_engine) -> None:
    from datetime import UTC, datetime, timedelta

    from app.email.outbox import drain_outbox_now, enqueue
    from app.email.sender import CaptureSender

    row_id = enqueue(
        kind="comment",
        template="order_comment",
        to="nakup@ukazkova.example.com",
        context={},
        locale="cs",
        tenant_id=demo.tenant_id,
    )
    sender = CaptureSender()
    await drain_outbox_now(sender, now=datetime.now(UTC) + timedelta(hours=1))
    assert sender.outbox == []
    async with owner_engine.connect() as conn:
        failed_at, last_error = (
            await conn.execute(
                text("SELECT failed_at, last_error FROM email_outbox WHERE id = :id"),
                {"id": row_id},
            )
        ).one()
    assert failed_at is not None and "public demo" in last_error


# ---------------------------------------------------------------- uploads


async def _upload(client, order_id, name: str, data: bytes, ctype: str):
    return await client.post(
        f"/app/orders/{order_id}/attachments",
        files={"file": (name, data, ctype)},
        follow_redirects=False,
    )


async def test_upload_rules_in_the_demo(demo_client, owner_engine, mock_s3) -> None:
    await _enter(demo_client, "customer")
    order_id = await _order_id(owner_engine, DEMO, "Konzole KM-120 – série 200 ks")

    ok = await _upload(demo_client, order_id, "foto.png", _png(), "image/png")
    assert ok.status_code == 303 and "notice=" in ok.headers["location"]

    pdf = b"%PDF-1.4\n" + b"0" * 1000
    ok = await _upload(demo_client, order_id, "vykres.pdf", pdf, "application/pdf")
    assert ok.status_code == 303 and "notice=" in ok.headers["location"]

    for name, data, ctype in (
        ("model.dxf", b"0\nSECTION\n" * 50, "application/dxf"),
        ("fake.pdf", b"MZ\x90\x00 not a pdf", "application/pdf"),
        ("big.pdf", b"%PDF-1.4\n" + b"0" * (2 * 1024 * 1024 + 10), "application/pdf"),
    ):
        resp = await _upload(demo_client, order_id, name, data, ctype)
        assert resp.status_code == 303, name
        assert resp.headers["location"].startswith(f"/app/orders/{order_id}?error="), name

    # Way past the cap: refused by the middleware before the body is read.
    huge = await _upload(
        demo_client, order_id, "huge.pdf", b"%PDF-" + b"0" * (4 * 1024 * 1024), "application/pdf"
    )
    assert huge.status_code == 303 and "error=" in huge.headers["location"]

    async with owner_engine.connect() as conn:
        stored = (
            (
                await conn.execute(
                    text("SELECT filename FROM order_attachments WHERE order_id = :o ORDER BY 1"),
                    {"o": order_id},
                )
            )
            .scalars()
            .all()
        )
    assert stored == ["foto.png", "vykres.pdf"]


async def test_upload_daily_cap(demo_client, owner_engine, demo, mock_s3) -> None:
    from app.demo.guard import DEMO_MAX_UPLOADS_PER_DAY

    await _enter(demo_client, "staff")
    order_id = await _order_id(owner_engine, DEMO, "Konzole KM-120 – série 200 ks")
    async with owner_engine.begin() as conn:
        for _ in range(DEMO_MAX_UPLOADS_PER_DAY - 1):
            await conn.execute(
                text(
                    "INSERT INTO audit_events (id, tenant_id, occurred_at, actor_type, "
                    "actor_label, action, entity_type, entity_id, entity_label, created_at, "
                    "updated_at) VALUES (gen_random_uuid(), :t, now(), 'user', 'x', "
                    "'attachment.upload', 'attachment', gen_random_uuid(), 'x.png', now(), now())"
                ),
                {"t": demo.tenant_id},
            )
    ok = await _upload(demo_client, order_id, "a.png", _png(), "image/png")
    assert "notice=" in ok.headers["location"]  # the 20th
    refused = await _upload(demo_client, order_id, "b.png", _png(), "image/png")
    assert "error=" in refused.headers["location"]  # the 21st


async def test_uploads_elsewhere_are_not_capped(settings, demo, owner_engine, mock_s3) -> None:
    other = await seed_demo(slug=OTHER, password=PASSWORD, engine=owner_engine, files=False)
    async with await _client(settings, OTHER) as client:
        await client.post("/auth/login", data={"email": other.staff_email, "password": PASSWORD})
        order_id = await _order_id(owner_engine, OTHER, "Konzole KM-120 – série 200 ks")
        dxf = await _upload(client, order_id, "model.dxf", b"0\nSECTION\n" * 50, "application/dxf")
        assert "notice=" in dxf.headers["location"]
        big = await _upload(
            client, order_id, "big.pdf", b"%PDF-1.4\n" + b"0" * (3 * 1024 * 1024), "application/pdf"
        )
        assert "notice=" in big.headers["location"]


# ------------------------------------------------------------------ reset


async def test_seed_with_files_attaches_watermarked_drawings(
    settings, owner_engine, wipe_db, mock_s3
) -> None:
    result = await seed_demo(slug=DEMO, password=PASSWORD, engine=owner_engine)
    assert result.attachments == 3
    async with owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT a.filename, a.storage_key, a.thumbnail_key, a.content_type, "
                    "a.created_at, o.created_at AS order_created "
                    "FROM order_attachments a JOIN orders o ON o.id = a.order_id "
                    "WHERE a.tenant_id = :t"
                ),
                {"t": result.tenant_id},
            )
        ).all()
    assert len(rows) == 3
    async with owner_engine.connect() as conn:
        recent_uploads = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE tenant_id = :t "
                    "AND action = 'attachment.upload' AND created_at >= now() - interval '1 day'"
                ),
                {"t": result.tenant_id},
            )
        ).scalar_one()
    assert recent_uploads == 0  # seeded drawings never eat the visitors' daily cap
    from app.storage import s3 as s3_storage

    for row in rows:
        assert row.content_type == "application/pdf"
        assert row.storage_key.startswith(f"tenants/{DEMO}/")
        data = s3_storage.download_bytes(row.storage_key)
        assert data.startswith(b"%PDF-") and len(data) < 200_000
        assert row.created_at > row.order_created  # uploaded with the order, not "today"
        import shutil

        if shutil.which("pdftoppm"):
            assert row.thumbnail_key is not None
            assert s3_storage.download_bytes(row.thumbnail_key)[:3] == b"\xff\xd8\xff"


async def test_seed_is_a_good_showcase(owner_engine, wipe_db) -> None:
    """Internally consistent: an overdue order, quotes awaiting the client,
    returned material, pending invitations the nightly cleanup keeps."""
    from datetime import date

    from app.tasks.periodic import INVITE_EXPIRY_DAYS

    result = await seed_demo(slug=DEMO, password=PASSWORD, engine=owner_engine, files=False)
    async with owner_engine.connect() as conn:
        tid = result.tenant_id
        overdue = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM orders WHERE tenant_id = :t AND promised_delivery_at < :d "
                    "AND status IN ('confirmed','in_production','ready')"
                ),
                {"t": tid, "d": date.today()},
            )
        ).scalar_one()
        quoted_for_customer = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM orders o JOIN customer_contacts c "
                    "ON c.customer_id = o.customer_id WHERE o.tenant_id = :t "
                    "AND o.status = 'quoted' AND c.email = :e"
                ),
                {"t": tid, "e": DEMO_CONTACT_EMAIL},
            )
        ).scalar_one()
        returned = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM asset_movements WHERE tenant_id = :t AND type = 'issue'"
                ),
                {"t": tid},
            )
        ).scalar_one()
        stale_invites = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM customer_contacts WHERE tenant_id = :t "
                    "AND accepted_at IS NULL AND invited_at < now() - make_interval(days => :d)"
                ),
                {"t": tid, "d": INVITE_EXPIRY_DAYS},
            )
        ).scalar_one()
        customer_statuses = {
            r[0]
            for r in (
                await conn.execute(
                    text(
                        "SELECT DISTINCT o.status FROM orders o JOIN customer_contacts c "
                        "ON c.customer_id = o.customer_id WHERE c.email = :e"
                    ),
                    {"e": DEMO_CONTACT_EMAIL},
                )
            ).all()
        }
        # The dashboard's "Recent activity" reads the audit trail.
        trail = dict(
            (
                await conn.execute(
                    text(
                        "SELECT action, count(*) FROM audit_events WHERE tenant_id = :t "
                        "AND occurred_at <= now() GROUP BY action"
                    ),
                    {"t": tid},
                )
            ).all()
        )
    assert trail.get("order.status_changed", 0) > 20
    assert trail.get("order.comment_added", 0) > 5
    assert overdue >= 1
    assert quoted_for_customer >= 1
    assert returned >= 1
    assert stale_invites == 0
    assert {"draft", "submitted", "quoted", "in_production", "ready", "delivered"} <= {
        s.lower() for s in customer_statuses
    }


async def test_reset_job_restores_the_seed_and_removes_visitor_files(
    settings, owner_engine, wipe_db, mock_s3
) -> None:
    from app.tasks.demo_reset import reset_public_demo

    seeded = await seed_demo(slug=DEMO, password=PASSWORD, engine=owner_engine)
    settings.public_demo_tenant = DEMO
    seeded_keys = _keys(f"tenants/{DEMO}/")
    assert seeded_keys

    # A visitor: one more order, an upload, a comment.
    async with await _client(settings, DEMO) as client:
        await _enter(client, "staff")
        resp = await client.post(
            "/app/orders",
            data={
                "title": "Návštěvník – testovací",
                "customer_id": await _customer_id(owner_engine),
            },
            follow_redirects=False,
        )
        new_order = resp.headers["location"].split("?")[0].rsplit("/", 1)[-1]
        up = await _upload(client, new_order, "foto.png", _png(), "image/png")
        assert "notice=" in up.headers["location"]
    visitor_keys = set(_keys(f"tenants/{DEMO}/")) - set(seeded_keys)
    assert visitor_keys

    first = await reset_public_demo(engine=owner_engine)
    second = await reset_public_demo(engine=owner_engine)  # idempotent
    assert first is not None and second is not None
    assert first["orders"] == second["orders"] == seeded.orders
    assert first["attachments"] == second["attachments"] == 3

    async with owner_engine.connect() as conn:
        tid = (
            await conn.execute(text("SELECT id FROM tenants WHERE slug = :s"), {"s": DEMO})
        ).scalar_one()
        assert tid == seeded.tenant_id  # same tenant, re-filled
        orders = (
            (await conn.execute(text("SELECT title FROM orders WHERE tenant_id = :t"), {"t": tid}))
            .scalars()
            .all()
        )
        db_keys = set(
            (
                await conn.execute(
                    text(
                        "SELECT storage_key FROM order_attachments WHERE tenant_id = :t "
                        "UNION SELECT thumbnail_key FROM order_attachments "
                        "WHERE tenant_id = :t AND thumbnail_key IS NOT NULL"
                    ),
                    {"t": tid},
                )
            )
            .scalars()
            .all()
        )
        attachments = (
            await conn.execute(
                text("SELECT count(*) FROM order_attachments WHERE tenant_id = :t"), {"t": tid}
            )
        ).scalar_one()
    assert len(orders) == seeded.orders
    assert "Návštěvník – testovací" not in orders
    assert attachments == 3
    stored = set(_keys(f"tenants/{DEMO}/"))
    assert stored == db_keys  # nothing left over: visitor files and old drawings are gone
    assert not (stored & visitor_keys)


async def test_reset_job_skips_and_refuses_safely(settings, owner_engine, wipe_db) -> None:
    from app.tasks.demo_reset import reset_public_demo

    settings.public_demo_tenant = ""
    assert await reset_public_demo(engine=owner_engine) is None

    settings.public_demo_tenant = "nobody-here"
    assert await reset_public_demo(engine=owner_engine) is None

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        session.add(
            Tenant(
                id=uuid4(),
                slug="real-shop",
                name="Real s.r.o.",
                billing_email="b@real.example.com",
                storage_prefix="tenants/real-shop/",
            )
        )
    settings.public_demo_tenant = "real-shop"
    assert await reset_public_demo(engine=owner_engine) is None
    async with sm() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.slug == "real-shop"))
        ).scalar_one()
        assert tenant.name == "Real s.r.o."  # untouched


def test_reset_job_is_scheduled_nightly_in_prague() -> None:
    from app.scheduler import build_scheduler

    job = build_scheduler().get_job("reset_public_demo")
    assert job is not None
    trigger = str(job.trigger)
    assert "hour='2'" in trigger and "minute='30'" in trigger
    assert str(job.trigger.timezone) == "Europe/Prague"


def test_unsafe_storage_prefix_is_refused() -> None:
    from app.demo.seed import tenant_storage_prefix

    assert tenant_storage_prefix("tenants/ukazka/") == "tenants/ukazka/"
    assert tenant_storage_prefix("tenants/ukazka") == "tenants/ukazka/"
    for bad in ("", "/", "  ", None):
        with pytest.raises(ValueError):
            tenant_storage_prefix(bad)


# -------------------------------------------------------------- marketing


def test_public_demo_url_is_derived_from_settings(settings) -> None:
    from app.urls import public_demo_url

    settings.app_base_url = "https://assoluto.eu"
    settings.default_tenant_slug = None
    settings.public_demo_tenant = ""
    assert public_demo_url(settings) == ""
    settings.public_demo_tenant = "ukazka"
    assert public_demo_url(settings) == "https://ukazka.assoluto.eu/demo"


async def test_marketing_pages_show_the_demo_button_only_when_configured(settings) -> None:
    settings.app_base_url = "https://assoluto.eu"
    settings.default_tenant_slug = None
    settings.feature_platform = True
    for configured in ("", "ukazka"):
        settings.public_demo_tenant = configured
        app = create_app(settings)
        async with CsrfAwareClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            for path in ("/", "/pricing"):
                resp = await client.get(path)
                assert resp.status_code == 200, path
                has_link = "https://ukazka.assoluto.eu/demo" in resp.text
                assert has_link is bool(configured), (path, configured)
