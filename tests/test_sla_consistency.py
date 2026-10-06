"""SLA cards and heatmap agree (demo review P1-1 / P3-4).

Fixed dates throughout: ``today`` is Wednesday 2026-10-07 and the window
is the last 30 days, so every bucket is pinned down independently of
the day the suite runs. See ``app.services.sla_service`` for the rules.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.customer import Customer
from app.models.enums import OrderStatus
from app.models.order import Order, OrderStatusHistory
from app.services import sla_service
from tests.test_sla_service import _app_session

pytestmark = pytest.mark.postgres

TODAY = date(2026, 10, 7)  # Wednesday
DATE_FROM = TODAY - timedelta(days=30)  # 2026-09-07, a Monday
PRAGUE = ZoneInfo("Europe/Prague")


async def _seed(owner_engine, tenant_id) -> None:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        acme = Customer(id=uuid4(), tenant_id=tenant_id, name="ACME")
        beta = Customer(id=uuid4(), tenant_id=tenant_id, name="Beta")
        session.add_all([acme, beta])
        await session.flush()

        def order(n, cust, status, promised, delivered=None, ready_at=None):
            o = Order(
                id=uuid4(),
                tenant_id=tenant_id,
                customer_id=cust.id,
                number=f"2026-{n:06d}",
                title=f"SLA {n}",
                status=status,
                promised_delivery_at=promised,
                delivered_at=delivered,
            )
            session.add(o)
            history = []
            if status in (OrderStatus.DELIVERED, OrderStatus.CLOSED):
                history.append(OrderStatusHistory(to_status=OrderStatus.DELIVERED))
            if ready_at is not None:
                history.append(OrderStatusHistory(to_status=OrderStatus.READY, created_at=ready_at))
            for h in history:
                h.tenant_id = tenant_id
                h.order_id = o.id
            return o, history

        rows = [
            # on time
            order(1, acme, OrderStatus.DELIVERED, date(2026, 9, 30), date(2026, 9, 29)),
            # late
            order(2, acme, OrderStatus.DELIVERED, date(2026, 9, 28), date(2026, 10, 1)),
            # overdue, still in production (another customer)
            order(3, beta, OrderStatus.IN_PRODUCTION, date(2026, 10, 2)),
            # due today, still open -> neutral
            order(4, acme, OrderStatus.IN_PRODUCTION, TODAY),
            # due later this week -> neutral, even though the week is shown
            order(5, acme, OrderStatus.CONFIRMED, date(2026, 10, 9)),
            # delivered early, promised in the future -> not judged yet
            order(6, acme, OrderStatus.DELIVERED, date(2026, 10, 20), date(2026, 10, 5)),
            # READY the day before the promise -> neutral (P3-4)
            order(
                7,
                acme,
                OrderStatus.READY,
                date(2026, 10, 1),
                ready_at=datetime(2026, 9, 30, 10, 0, tzinfo=UTC),
            ),
            # READY two days after the promise -> overdue
            order(
                8,
                acme,
                OrderStatus.READY,
                date(2026, 10, 1),
                ready_at=datetime(2026, 10, 3, 10, 0, tzinfo=UTC),
            ),
            # READY at 22:30 UTC on the promised day = 00:30 next day in
            # Prague -> became ready a day late -> overdue
            order(
                9,
                acme,
                OrderStatus.READY,
                date(2026, 10, 1),
                ready_at=datetime(2026, 10, 1, 22, 30, tzinfo=UTC),
            ),
            # READY with no READY history entry (e.g. imported) -> overdue
            order(10, beta, OrderStatus.READY, date(2026, 9, 21)),
            # cancelled -> never counts
            order(11, acme, OrderStatus.CANCELLED, date(2026, 9, 29)),
            # promised before the window -> outside
            order(12, acme, OrderStatus.DELIVERED, date(2026, 8, 3), date(2026, 8, 10)),
        ]
        for o, _h in rows:
            session.add(o)
        await session.flush()
        for _o, history in rows:
            session.add_all(history)


async def _run(tenant_id, coro_factory):
    engine, sm = await _app_session(tenant_id)
    try:
        async with sm() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(tenant_id)},
            )
            return await coro_factory(session)
    finally:
        await engine.dispose()


async def test_cards_count_due_orders_only(owner_engine, demo_tenant) -> None:
    await _seed(owner_engine, demo_tenant.id)
    summary = await _run(
        demo_tenant.id,
        lambda s: sla_service.on_time_rate(
            s, date_from=DATE_FROM, date_to=TODAY, today=TODAY, tz=PRAGUE
        ),
    )
    # on time: #1; late: #2; overdue: #3, #8, #9, #10.
    assert summary == {
        "on_time": 1,
        "late": 1,
        "pending": 4,
        "delivered": 2,
        "total": 6,
        "rate": pytest.approx(1 / 6),
    }


async def test_date_to_in_the_future_is_clamped_to_today(owner_engine, demo_tenant) -> None:
    await _seed(owner_engine, demo_tenant.id)
    summary = await _run(
        demo_tenant.id,
        lambda s: sla_service.on_time_rate(
            s, date_from=DATE_FROM, date_to=TODAY + timedelta(days=60), today=TODAY, tz=PRAGUE
        ),
    )
    # #6 (delivered early, promised 20 Oct) is still not judged.
    assert summary["on_time"] == 1
    assert summary["total"] == 6


async def test_ready_on_time_turns_overdue_in_a_far_zone(owner_engine, demo_tenant) -> None:
    """The READY day is the tenant's local day: in New York, #9 became
    ready on 1 Oct (18:30 local) — on its promised day, so not overdue."""
    await _seed(owner_engine, demo_tenant.id)
    summary = await _run(
        demo_tenant.id,
        lambda s: sla_service.on_time_rate(
            s,
            date_from=DATE_FROM,
            date_to=TODAY,
            today=TODAY,
            tz=ZoneInfo("America/New_York"),
        ),
    )
    assert summary["pending"] == 3


async def test_heatmap_cells_add_up_to_the_cards(owner_engine, demo_tenant) -> None:
    await _seed(owner_engine, demo_tenant.id)

    async def both(s):
        cards = await sla_service.on_time_rate(
            s, date_from=DATE_FROM, date_to=TODAY, today=TODAY, tz=PRAGUE
        )
        cells = await sla_service.heatmap_data(s, date_from=DATE_FROM, today=TODAY, tz=PRAGUE)
        return cards, cells

    cards, cells = await _run(demo_tenant.id, both)
    assert sum(c["on_time"] for c in cells) == cards["on_time"]
    assert sum(c["late"] for c in cells) == cards["late"]
    assert sum(c["overdue"] for c in cells) == cards["pending"]
    assert sum(c["total"] for c in cells) == cards["total"]

    by_key = {(c["customer_name"], c["week_start"]): c for c in cells}
    assert set(by_key) == {
        ("ACME", date(2026, 9, 28)),
        ("Beta", date(2026, 9, 28)),
        ("Beta", date(2026, 9, 21)),
    }
    acme = by_key[("ACME", date(2026, 9, 28))]
    assert (acme["on_time"], acme["late"], acme["overdue"], acme["total"]) == (1, 1, 2, 4)
    # Nothing judged in the current week (#4, #5 are not overdue yet).
    assert all(c["week_start"] < date(2026, 10, 5) for c in cells)


def test_heatmap_weeks_are_continuous_and_stop_at_this_week() -> None:
    weeks = sla_service.heatmap_weeks(DATE_FROM, TODAY)
    assert weeks == [
        date(2026, 9, 7),
        date(2026, 9, 14),
        date(2026, 9, 21),
        date(2026, 9, 28),
        date(2026, 10, 5),
    ]
    # A window starting mid-week starts at that week's Monday.
    assert sla_service.heatmap_weeks(date(2026, 9, 10), TODAY)[0] == date(2026, 9, 7)
    # A year back: 53 consecutive Mondays, none in the future.
    year = sla_service.heatmap_weeks(TODAY - timedelta(days=365), TODAY)
    assert all(b - a == timedelta(weeks=1) for a, b in pairwise(year))
    assert year[-1] == date(2026, 10, 5)


async def test_sla_page_renders_one_definition(tenant_client, owner_engine, demo_tenant) -> None:
    """End to end with the real clock: an overdue order makes the card read
    0 %, the heatmap axis covers every week of the window and no later."""
    from app.timezones import local_today
    from tests.test_orders_item_autosave import _login
    from tests.test_orders_item_autosave import _seed as _seed_users

    seed = await _seed_users(owner_engine, demo_tenant.id)
    today = local_today(PRAGUE)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        session.add(
            Order(
                id=uuid4(),
                tenant_id=demo_tenant.id,
                customer_id=seed["acme"].id,
                number="2026-000901",
                title="Overdue",
                status=OrderStatus.IN_PRODUCTION,
                promised_delivery_at=today - timedelta(days=3),
            )
        )
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    body = (await tenant_client.get("/app/admin/sla?timeframe=30")).text

    assert "0\xa0%" in body  # cs, NBSP before the sign
    weeks = sla_service.heatmap_weeks(today - timedelta(days=30), today)
    # Every week is a column header (title="<Week of> dd.mm.yyyy"), in order.
    positions = [body.index(f' {week.strftime("%d.%m.%Y")}">') for week in weeks]
    assert positions == sorted(positions)
    next_monday = weeks[-1] + timedelta(weeks=1)
    assert f' {next_monday.strftime("%d.%m.%Y")}">' not in body
    # ISO dates are gone from the page header.
    assert (today - timedelta(days=30)).isoformat() not in body
    assert (today - timedelta(days=30)).strftime("%d.%m.%Y") in body
    # N6: the rate is one sentence with the number in it, so Czech can
    # make the verb agree ("0 % zakázek … dodrželo …").
    en = (
        await tenant_client.get("/app/admin/sla?timeframe=30", headers={"Accept-Language": "en"})
    ).text
    assert "0% of orders due in this period met the promised date" in en
    assert ">\n                    of orders due" not in en
