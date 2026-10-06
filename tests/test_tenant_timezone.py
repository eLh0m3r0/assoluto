"""Tenant time zones (decision E2, audit 2026-10-03 LOGIC-16).

Everything is stored in UTC; everything a person reads — pages, PDFs,
CSV — and every calendar-day question (date filters, "today", the
delivered stamp, the order-number year) is answered in the tenant's
zone, ``tenants.settings["timezone"]``, default ``Europe/Prague``.

The fixed instants below straddle the two traps:

* **DST** — Europe/Prague leaves summer time on 2026-10-25 at 03:00
  CEST (01:00 UTC); 2026-10-25 is 25 hours long.
* **Midnight** — 23:30 UTC is already the next day in Prague.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.models.audit_event import AuditEvent
from app.models.customer import Customer
from app.models.enums import OrderStatus, UserRole
from app.models.order import Order, OrderComment, OrderItem
from app.models.tenant import Tenant
from app.models.user import User
from app.security.passwords import hash_password
from app.templating import _new_environment
from app.timezones import (
    CURATED_TIMEZONES,
    format_local,
    local_date,
    local_day_start,
    local_today,
    normalize_timezone,
    tenant_tz,
    timezone_choices,
    tz_label,
)

PRAGUE = "Europe/Prague"
# 23:30 UTC on 2 March = 00:30 CET on 3 March.
LATE_EVENING_UTC = datetime(2026, 3, 2, 23, 30, tzinfo=UTC)


def _tenant(tz: object = None) -> SimpleNamespace:
    return SimpleNamespace(settings={} if tz is None else {"timezone": tz})


# --------------------------------------------------------------- unit: core


def test_normalize_accepts_iana_names_only() -> None:
    assert normalize_timezone("Europe/Prague") == "Europe/Prague"
    assert normalize_timezone("  America/New_York ") == "America/New_York"
    assert normalize_timezone("UTC") == "UTC"
    for bad in ("", "Mars/Olympus_Mons", "../../etc/passwd", "/etc/localtime", None, 42):
        assert normalize_timezone(bad) is None, bad


def test_default_is_prague_and_invalid_or_missing_falls_back() -> None:
    assert tz_label(tenant_tz(_tenant())) == PRAGUE
    assert tz_label(tenant_tz(None)) == PRAGUE
    assert tz_label(tenant_tz(_tenant("Bogus/Zone"))) == PRAGUE
    assert tz_label(tenant_tz(_tenant(123))) == PRAGUE
    assert tz_label(tenant_tz(SimpleNamespace(settings=None))) == PRAGUE
    assert tz_label(tenant_tz(_tenant("America/New_York"))) == "America/New_York"


def test_broken_default_timezone_setting_falls_back_to_prague(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "default_timezone", "Not/AZone")
    assert tz_label(tenant_tz(None)) == PRAGUE
    monkeypatch.setattr(get_settings(), "default_timezone", "Europe/London")
    assert tz_label(tenant_tz(None)) == "Europe/London"


def test_late_evening_utc_is_next_day_in_prague() -> None:
    tz = tenant_tz(_tenant())
    assert local_date(LATE_EVENING_UTC, tz) == date(2026, 3, 3)
    assert format_local(LATE_EVENING_UTC, tz) == "03.03.2026 00:30"
    # Same instant in New York is still the 2nd.
    ny = tenant_tz(_tenant("America/New_York"))
    assert local_date(LATE_EVENING_UTC, ny) == date(2026, 3, 2)
    # "Today" follows the zone, not the server clock.
    assert local_today(tz, now=LATE_EVENING_UTC) == date(2026, 3, 3)


def test_naive_datetimes_are_taken_as_utc() -> None:
    tz = tenant_tz(_tenant())
    naive = datetime(2026, 3, 2, 23, 30)
    assert format_local(naive, tz) == "03.03.2026 00:30"


def test_plain_dates_are_not_shifted() -> None:
    tz = tenant_tz(_tenant("Pacific/Kiritimati"))  # UTC+14
    assert format_local(date(2026, 3, 2), tz, "%d.%m.%Y") == "02.03.2026"


def test_dst_end_2026_10_25() -> None:
    """02:30 happens twice on 2026-10-25 in Prague; the abbreviation tells them apart."""
    tz = tenant_tz(_tenant())
    fmt = "%Y-%m-%d %H:%M %Z"
    assert format_local(datetime(2026, 10, 25, 0, 30, tzinfo=UTC), tz, fmt) == (
        "2026-10-25 02:30 CEST"
    )
    assert format_local(datetime(2026, 10, 25, 1, 30, tzinfo=UTC), tz, fmt) == (
        "2026-10-25 02:30 CET"
    )
    # Day boundaries around the change: 25 Oct starts at 22:00 UTC the day
    # before (still CEST) and 26 Oct at 23:00 UTC (CET) — a 25-hour day.
    start_25 = local_day_start(date(2026, 10, 25), tz)
    start_26 = local_day_start(date(2026, 10, 26), tz)
    assert start_25 == datetime(2026, 10, 24, 22, 0, tzinfo=UTC)
    assert start_26 == datetime(2026, 10, 25, 23, 0, tzinfo=UTC)
    assert (start_26 - start_25).total_seconds() == 25 * 3600


def test_dst_start_2026_03_29() -> None:
    tz = tenant_tz(_tenant())
    assert format_local(datetime(2026, 3, 29, 0, 59, tzinfo=UTC), tz, "%H:%M %Z") == "01:59 CET"
    assert format_local(datetime(2026, 3, 29, 1, 0, tzinfo=UTC), tz, "%H:%M %Z") == "03:00 CEST"
    start = local_day_start(date(2026, 3, 29), tz)
    end = local_day_start(date(2026, 3, 30), tz)
    assert (end - start).total_seconds() == 23 * 3600


def test_timezone_choices_curated_first_then_the_rest() -> None:
    choices = timezone_choices()
    assert choices[: len(CURATED_TIMEZONES)] == CURATED_TIMEZONES
    assert "America/New_York" in choices
    assert "Asia/Tokyo" in choices
    assert len(choices) == len(set(choices))
    rest = list(choices[len(CURATED_TIMEZONES) :])
    assert rest == sorted(rest)


def test_jinja_filters_use_display_tz() -> None:
    env = _new_environment(None)
    tpl = env.from_string(
        "{{ ts|localtime }}|{{ ts|localdate }}|{{ ts|localtime('iso') }}|{{ d|localdate }}"
    )
    out = tpl.render(ts=LATE_EVENING_UTC, d=date(2026, 3, 2), display_tz=tenant_tz(_tenant()))
    assert out == "03.03.2026 00:30|03.03.2026|2026-03-03T00:30:00+01:00|02.03.2026"
    # No zone in the context (tenant-less render) → DEFAULT_TIMEZONE.
    assert env.from_string("{{ ts|localtime }}").render(ts=LATE_EVENING_UTC) == ("03.03.2026 00:30")
    assert env.from_string("{{ none|localtime }}").render() == ""


# ------------------------------------------------------------------ unit: PDF


def _pdf_fixture(tz: str | None):
    tenant = Tenant(
        id=uuid4(),
        slug="4mex",
        name="4MEX s.r.o.",
        billing_email="b@b.cz",
        storage_prefix="tenants/4mex/",
        settings={} if tz is None else {"timezone": tz},
    )
    customer = Customer(id=uuid4(), tenant_id=tenant.id, name="ACME")
    order = Order(
        id=uuid4(),
        tenant_id=tenant.id,
        customer_id=customer.id,
        number="2026-000042",
        title="TZ",
        status=OrderStatus.QUOTED,
        quoted_total=Decimal("100.00"),
        currency="CZK",
    )
    order.created_at = LATE_EVENING_UTC
    order.submitted_at = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    order.promised_delivery_at = None
    item = OrderItem(
        id=uuid4(),
        tenant_id=tenant.id,
        order_id=order.id,
        position=0,
        description="Díl",
        quantity=Decimal("1"),
        unit="ks",
        unit_price=Decimal("100"),
        line_total=Decimal("100.00"),
    )
    return order, [item], customer, tenant


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="poppler-utils not installed")
@pytest.mark.parametrize(
    ("tz", "created", "submitted", "zones"),
    [
        (None, "03.03.2026 00:30", "25.10.2026 02:30", ("CET", "CEST")),
        ("America/New_York", "02.03.2026 18:30", "24.10.2026 21:30", ("EST", "EDT")),
    ],
)
def test_pdf_prints_local_times_and_zone_in_footer(tz, created, submitted, zones) -> None:
    from app.services.pdf_service import render_order_pdf

    order, items, customer, tenant = _pdf_fixture(tz)
    pdf = render_order_pdf(order, items, customer, tenant, locale="en")
    out = subprocess.run(["pdftotext", "-", "-"], input=pdf, capture_output=True, check=True)
    body = out.stdout.decode()
    assert created in body
    assert submitted in body
    assert "UTC" not in body
    footer = re.search(r"Generated — \d{2}\.\d{2}\.\d{4} \d{2}:\d{2} ([A-Z]+)", body)
    assert footer is not None, body
    assert footer.group(1) in zones


# ------------------------------------------------------------------ Postgres


postgres_only = pytest.mark.postgres


async def _set_tz(owner_engine, tenant_id, tz: str | None) -> None:
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE tenants SET settings = CAST(:s AS JSONB) WHERE id = :id"),
            {"s": "{}" if tz is None else f'{{"timezone": "{tz}"}}', "id": tenant_id},
        )


async def _seed(owner_engine, tenant_id) -> dict:
    """Admin + customer + two orders: 2 Mar 23:30 UTC and 3 Mar 23:30 UTC."""
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        staff = User(
            id=uuid4(),
            tenant_id=tenant_id,
            email="owner@4mex.cz",
            full_name="Owner",
            role=UserRole.TENANT_ADMIN,
            password_hash=hash_password("ownerpass"),
        )
        cust = Customer(id=uuid4(), tenant_id=tenant_id, name="ACME")
        session.add_all([staff, cust])
        await session.flush()
        early = Order(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=cust.id,
            number="2026-000001",
            title="Night owl",
            status=OrderStatus.SUBMITTED,
            created_at=LATE_EVENING_UTC,
            submitted_at=LATE_EVENING_UTC,
        )
        late = Order(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=cust.id,
            number="2026-000002",
            title="Next evening",
            status=OrderStatus.SUBMITTED,
            created_at=datetime(2026, 3, 3, 23, 30, tzinfo=UTC),
        )
        session.add_all([early, late])
        await session.flush()
        session.add(
            OrderComment(
                id=uuid4(),
                tenant_id=tenant_id,
                order_id=early.id,
                author_user_id=staff.id,
                body="Across the DST change",
                created_at=datetime(2026, 10, 25, 1, 30, tzinfo=UTC),
            )
        )
        return {"staff_id": staff.id, "early_id": early.id, "late_id": late.id}


async def _login(client: AsyncClient) -> None:
    resp = await client.post(
        "/auth/login",
        data={"email": "owner@4mex.cz", "password": "ownerpass"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


@postgres_only
async def test_order_list_shows_local_date(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client)
    body = (await tenant_client.get("/app/orders")).text
    # 2 Mar 23:30 UTC is 3 Mar in Prague; the UTC date must not appear.
    assert "03.03.2026" in body
    assert "02.03.2026" not in body


@postgres_only
async def test_order_detail_comment_time_is_local_with_dst(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    ids = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client)
    body = (await tenant_client.get(f"/app/orders/{ids['early_id']}")).text
    # 01:30 UTC on 25 Oct = 02:30 CET (after the clocks went back).
    assert "25.10.2026 02:30" in body
    assert "25.10.2026 01:30" not in body


@postgres_only
async def test_setting_change_takes_effect_is_audited_and_flashes(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    ids = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client)

    page = (await tenant_client.get("/app/admin/tenant-settings")).text
    assert 'name="timezone"' in page
    assert '<option value="Europe/Prague" selected>' in page

    resp = await tenant_client.post(
        "/app/admin/tenant-settings",
        data={"default_locale": "cs", "timezone": "America/New_York"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "saved=1" in resp.headers["location"]

    page = (await tenant_client.get(resp.headers["location"])).text
    assert 'role="status"' in page  # the "Saved." flash
    assert '<option value="America/New_York" selected>' in page

    # 2 Mar 23:30 UTC is still 2 Mar (18:30) in New York.
    detail = (await tenant_client.get(f"/app/orders/{ids['early_id']}")).text
    assert "24.10.2026 21:30" in detail  # the DST comment, now in EDT
    listing = (await tenant_client.get("/app/orders")).text
    assert "02.03.2026" in listing

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        stored = (
            await session.execute(select(Tenant.settings).where(Tenant.id == demo_tenant.id))
        ).scalar_one()
        assert stored["timezone"] == "America/New_York"
        assert stored["default_locale"] == "cs"
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.action == "tenant.settings_updated")
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    assert events[0].diff is not None
    assert "America/New_York" in str(events[0].diff)


@postgres_only
async def test_invalid_timezone_is_rejected_and_nothing_saved(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client)
    resp = await tenant_client.post(
        "/app/admin/tenant-settings",
        data={"default_locale": "en", "timezone": "Mars/Olympus_Mons"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert 'role="alert"' in resp.text
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        stored = (
            await session.execute(select(Tenant.settings).where(Tenant.id == demo_tenant.id))
        ).scalar_one()
    assert "timezone" not in stored
    assert "default_locale" not in stored


@postgres_only
async def test_stored_garbage_timezone_falls_back_to_default(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _set_tz(owner_engine, demo_tenant.id, "Bogus/Zone")
    await _login(tenant_client)
    body = (await tenant_client.get("/app/orders")).text
    assert "03.03.2026" in body  # rendered in Europe/Prague
    page = (await tenant_client.get("/app/admin/tenant-settings")).text
    assert '<option value="Europe/Prague" selected>' in page


@postgres_only
async def test_csv_is_local_with_zone_in_header_and_local_date_filters(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client)

    resp = await tenant_client.get("/app/orders.csv")
    assert resp.status_code == 200
    lines = resp.text.lstrip("﻿").split("\r\n")
    header = lines[0].split(";")
    assert header[3].endswith("(Europe/Prague)")
    assert header[4].endswith("(Europe/Prague)")
    row = next(line for line in lines if line.startswith("2026-000001"))
    cols = row.split(";")
    assert cols[3] == "03.03.2026 00:30"  # cs: day-first, local
    assert cols[4] == "03.03.2026 00:30"

    # 3 March (local) = [2 Mar 23:00 UTC, 3 Mar 23:00 UTC): only the early order.
    one_day = (await tenant_client.get("/app/orders.csv?from=2026-03-03&to=2026-03-03")).text
    assert "2026-000001" in one_day
    assert "2026-000002" not in one_day  # 3 Mar 23:30 UTC is 4 Mar locally
    # 2 March (local) holds neither — under UTC both bounds would be wrong.
    assert (
        "2026-0000"
        not in (await tenant_client.get("/app/orders.csv?from=2026-03-02&to=2026-03-02")).text
    )
    four = (await tenant_client.get("/app/orders.csv?from=2026-03-04")).text
    assert "2026-000002" in four
    assert "2026-000001" not in four

    # Another zone, another day: New York sees both on 2/3 March.
    await _set_tz(owner_engine, demo_tenant.id, "America/New_York")
    ny = (await tenant_client.get("/app/orders.csv?from=2026-03-02&to=2026-03-02")).text
    assert "2026-000001" in ny
    assert "(America/New_York)" in ny.split("\r\n", 1)[0]
    assert "02.03.2026 18:30" in ny


@postgres_only
async def test_audit_log_filter_and_display_are_local(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    ids = await _seed(owner_engine, demo_tenant.id)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        session.add(
            AuditEvent(
                id=uuid4(),
                tenant_id=demo_tenant.id,
                occurred_at=LATE_EVENING_UTC,
                actor_type="system",
                actor_id=None,
                actor_label="system",
                action="order.status_changed",
                entity_type="order",
                entity_id=ids["early_id"],
                entity_label="tz-probe-event",
            )
        )
    await _login(tenant_client)
    hit = (await tenant_client.get("/app/admin/audit?from=2026-03-03&to=2026-03-03")).text
    assert "tz-probe-event" in hit
    assert "03.03.2026 00:30" in hit
    assert "03.03.2026 00:30 CET" in hit  # zone in the title tooltip
    miss = (await tenant_client.get("/app/admin/audit?from=2026-03-02&to=2026-03-02")).text
    assert "tz-probe-event" not in miss


class _FrozenDatetime(datetime):
    """``datetime`` whose ``now()`` is pinned; patched into a module."""

    frozen: datetime = LATE_EVENING_UTC

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        # An instance of the subclass, so ``isinstance(x, datetime)`` still
        # holds inside the patched module.
        return cls.fromtimestamp(cls.frozen.timestamp(), tz or UTC)


@postgres_only
async def test_order_number_year_is_local_new_year(owner_engine, demo_tenant, monkeypatch) -> None:
    """00:30 on 1 January in Prague (still 31 Dec in UTC) numbers into the new year."""
    import app.timezones as tzmod
    from app.services.order_service import _next_order_number

    _FrozenDatetime.frozen = datetime(2026, 12, 31, 23, 30, tzinfo=UTC)
    monkeypatch.setattr(tzmod, "datetime", _FrozenDatetime)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        number = await _next_order_number(session, tenant_id=demo_tenant.id)
    assert number == "2027-000001"

    await _set_tz(owner_engine, demo_tenant.id, "UTC")
    async with sm() as session, session.begin():
        number = await _next_order_number(session, tenant_id=demo_tenant.id)
    assert number == "2026-000001"


@postgres_only
async def test_delivered_at_is_the_tenant_local_day(owner_engine, demo_tenant, monkeypatch) -> None:
    """Delivered at 00:30 local on 26 Oct (23:30 UTC on the 25th) → 26 Oct."""
    import app.services.order_service as order_service
    from app.services.order_service import ActorRef, transition_order

    ids = await _seed(owner_engine, demo_tenant.id)
    _FrozenDatetime.frozen = datetime(2026, 10, 25, 23, 30, tzinfo=UTC)
    monkeypatch.setattr(order_service, "datetime", _FrozenDatetime)

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        order = (
            await session.execute(select(Order).where(Order.id == ids["early_id"]))
        ).scalar_one()
        await transition_order(
            session,
            order=order,
            to_status=OrderStatus.DELIVERED,
            actor=ActorRef(type="user", id=ids["staff_id"]),
            allow_incomplete=True,
        )
    async with sm() as session:
        got = (await session.execute(select(Order).where(Order.id == ids["early_id"]))).scalar_one()
    assert got.delivered_at == date(2026, 10, 26)
