"""The demo data survives a skeptical job-shop owner's two-minute click-through.

Findings of the 2026-10-06 public-demo review (P1-3, P1-4, P1-5, P2-8 …
P2-13, P3-1 … P3-15): comments anchored to the status they describe,
every timestamp on a Czech working day 07:00–16:30, no double finishing
charges, the client confirms quotes, a believable SLA, quotes with
promised dates, people with names instead of job titles.

The first tests check the plan (pure, no database) at awkward reset
times; the rest seed a tenant and check what a visitor actually sees.
"""

# ruff: noqa: RUF002

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.demo import seed as demo_seed
from app.models.enums import OrderStatus

PRAGUE = ZoneInfo("Europe/Prague")

#: An item whose *name* already says it comes finished (painted, galvanised…).
FINISH_WORDS = ("lakovan", "lak ral", "zinkovan", "pozink", "kartáčovan", "eloxovan")
#: A separate finishing line.
FINISH_LINES = ("Práškové lakování", "Žárové zinkování", "Kartáčování")


def _prague(year: int, month: int, day: int, hh: int, mm: int) -> datetime:
    return datetime(year, month, day, hh, mm, tzinfo=PRAGUE).astimezone(UTC)


def _in_business_hours(at: datetime) -> bool:
    local = at.astimezone(PRAGUE)
    return demo_seed.is_working_day(local.date()) and time(7, 0) <= local.time() <= time(16, 30)


def _last_reset(now: datetime) -> datetime:
    """The most recent 02:30 Europe/Prague — when the nightly reset runs."""
    local = now.astimezone(PRAGUE)
    reset = datetime.combine(local.date(), time(2, 30), tzinfo=PRAGUE)
    if reset > local:
        reset -= timedelta(days=1)
    return reset.astimezone(UTC)


# ------------------------------------------------------------------ plan


def test_working_day_clock() -> None:
    holidays = demo_seed.czech_holidays(2026)
    assert {date(2026, 4, 3), date(2026, 4, 6), date(2026, 9, 28), date(2026, 10, 28)} <= holidays
    anchor = demo_seed.anchor_day
    # The nightly reset (02:30) builds on the previous working day …
    assert anchor(_prague(2026, 10, 6, 2, 30)) == date(2026, 10, 5)
    # … on Friday after a weekend, and skips public holidays.
    assert anchor(_prague(2026, 10, 5, 2, 30)) == date(2026, 10, 2)
    assert anchor(_prague(2026, 9, 29, 2, 30)) == date(2026, 9, 25)
    assert anchor(_prague(2026, 4, 7, 2, 30)) == date(2026, 4, 2)
    # Mid-morning: today is not over, so its afternoon is not borrowed.
    assert anchor(_prague(2026, 10, 7, 10, 0)) == date(2026, 10, 6)
    # After closing time today counts.
    assert anchor(_prague(2026, 10, 9, 17, 5)) == date(2026, 10, 9)
    assert demo_seed.shift_working_days(date(2026, 10, 2), 1) == date(2026, 10, 5)
    assert demo_seed.shift_working_days(date(2026, 10, 29), -1) == date(2026, 10, 27)


@pytest.mark.parametrize(
    "now",
    [
        _prague(2026, 10, 6, 2, 30),  # an ordinary nightly reset
        _prague(2026, 10, 5, 2, 30),  # Monday after a weekend
        _prague(2026, 9, 29, 2, 30),  # the day after a public holiday
        _prague(2026, 4, 7, 2, 30),  # after Easter
        _prague(2026, 10, 7, 10, 15),  # an operator re-seeds mid-morning
        _prague(2027, 1, 4, 2, 30),  # across the year boundary
    ],
)
def test_plan_is_plausible_at_any_reset_time(now: datetime) -> None:
    clock = demo_seed.DemoClock(now)
    planned = demo_seed.plan_orders(clock)
    today = now.astimezone(PRAGUE).date()
    assert len(planned) == len(demo_seed.ORDERS)
    for p in planned:
        title = p.spec.title
        for event, at in zip(p.spec.timeline, p.times, strict=True):
            assert _in_business_hours(at), (title, event, at.astimezone(PRAGUE))
            assert at <= now, (title, event)
            if isinstance(event, demo_seed.Upload):
                # Never counted by the public demo's rolling 24 h upload cap.
                assert at < now - timedelta(days=1), (title, event)
        assert p.times == sorted(p.times), title
        assert len(set(p.times)) == len(p.times), title
        for d in (p.requested, p.promised, p.delivered):
            assert d is None or demo_seed.is_working_day(d), (title, d)
        if p.requested and p.promised:
            assert p.promised <= p.requested, title  # never promise later than asked
        if p.spec.status == OrderStatus.READY:
            assert p.promised is not None and p.promised >= today, title  # P3-4
    # Order numbers follow creation time (P3-9).
    numbers = [p.number for p in sorted(planned, key=lambda p: p.created)]
    assert numbers == sorted(numbers)
    assert len(set(numbers)) == len(numbers)
    # Material movements are in business hours too.
    for spec in demo_seed.ASSETS:
        for at in clock.slots([m.wd for m in spec.moves], spec.code):
            assert _in_business_hours(at) and at <= now, spec.code


def test_specs_tell_a_consistent_story() -> None:
    """Spec-level rules: who does what, why cancelled, drawings where mentioned."""
    contact_keys = {
        key: {p.local for p in people} for key, (_n, _d, people) in demo_seed.CUSTOMERS.items()
    }
    for o in demo_seed.ORDERS:
        # P3-1: no test-data titles; a cancellation says why.
        assert not any(w in o.title.lower() for w in ("zrušeno", "rozpracováno", "minulá dávka"))
        if o.status == OrderStatus.CANCELLED:
            assert o.steps[-1].note, o.title
            # N3: the client asks for the cancellation *before* the shop
            # carries it out — never a request posted after the fact.
            cancel_at = o.timeline.index(o.steps[-1])
            asked = [
                e
                for e in o.timeline[:cancel_at]
                if isinstance(e, demo_seed.Comment) and e.by in contact_keys[o.client]
            ]
            assert asked and "storn" in asked[-1].body, o.title
            assert not [
                e
                for e in o.timeline[cancel_at:]
                if isinstance(e, demo_seed.Comment) and e.by in contact_keys[o.client]
            ], o.title
        # P2-8: the client submits and confirms, the planner quotes, the
        # foreman runs the shop floor.
        for step in o.steps:
            if step.status in (OrderStatus.SUBMITTED, OrderStatus.CONFIRMED):
                assert step.by in contact_keys[o.client], (o.title, step)
            if step.status == OrderStatus.QUOTED:
                assert step.by == "planner", (o.title, step)
            if step.status in (OrderStatus.IN_PRODUCTION, OrderStatus.READY):
                assert step.by == "foreman", (o.title, step)
        # P2-12: a comment that talks about a drawing comes with one.
        if any(isinstance(e, demo_seed.Comment) and "výkres" in e.body.lower() for e in o.timeline):
            assert any(isinstance(e, demo_seed.Upload) for e in o.timeline), o.title
    with_files = [
        o for o in demo_seed.ORDERS if any(isinstance(e, demo_seed.Upload) for e in o.timeline)
    ]
    assert 8 <= len(with_files) <= 16
    titles = {o.title for o in demo_seed.ORDERS}
    assert {demo_seed.FLAGSHIP_ORDER_TITLE, demo_seed.OVERDUE_ORDER_TITLE} <= titles
    # P3-2: people have names, not job titles.
    jobs = ("Vedoucí", "Nákupčí", "Zásobovač", "Kvalitářka", "Objednávkář", "Výrobní",
            "Technolog", "Majitel", "Nákupní", "Dodavatelský", "Logistická", "Plánovačka",
            "Mistr", "Konstruktér")  # fmt: skip
    names = [s.name for s in demo_seed.STAFF] + [
        p.name for _n, _d, people in demo_seed.CUSTOMERS.values() for p in people
    ]
    assert not [n for n in names if any(j in n for j in jobs)]
    # P3-15: material names do not repeat "(materiál zákazníka)".
    assert not [a.name for a in demo_seed.ASSETS if "materiál zákazníka" in a.name]


def test_a_broken_timeline_is_refused() -> None:
    """The validator catches the stories the review complained about."""
    item = (demo_seed.Item("OHYB", "1"),)
    S, C = demo_seed.Step, demo_seed.Comment
    bad = [
        # The comment about production predates production.
        (S(OrderStatus.DRAFT, 5, "nakup"), C(OrderStatus.IN_PRODUCTION, 5, "planner", "x")),
        # The shop confirms its own quote.
        (
            S(OrderStatus.DRAFT, 5, "nakup"),
            S(OrderStatus.QUOTED, 4, "planner"),
            S(OrderStatus.CONFIRMED, 3, "owner"),
        ),
        # Out of order in time.
        (S(OrderStatus.DRAFT, 3, "nakup"), S(OrderStatus.SUBMITTED, 4, "nakup")),
    ]
    for timeline in bad:
        spec = demo_seed.OrderSpec("ukazkova", "x", item, timeline, promised=3)
        with pytest.raises(ValueError):
            demo_seed._validate(spec)
    quote_without_date = demo_seed.OrderSpec(
        "ukazkova",
        "x",
        item,
        (S(OrderStatus.DRAFT, 5, "nakup"), S(OrderStatus.QUOTED, 4, "planner")),
    )
    with pytest.raises(ValueError):
        demo_seed._validate(quote_without_date)


# ------------------------------------------------------------- seeded DB


async def _seeded(owner_engine) -> tuple[demo_seed.SeedResult, datetime]:
    """Seed as the nightly reset does: at the last 02:30 in Prague."""
    now = _last_reset(datetime.now(UTC))
    result = await demo_seed.seed_demo(
        slug="demo-real", password="Demo-heslo-1", engine=owner_engine, files=False, now=now
    )
    return result, now


async def _rows(conn, sql: str, tid, **params) -> list:
    return list((await conn.execute(text(sql), {"t": tid, **params})).all())


@pytest.mark.postgres
async def test_every_timestamp_is_in_business_hours(owner_engine, wipe_db) -> None:
    """P1-4: nothing at 02:30, nothing on a Sunday, no "yesterday 23:30"."""
    result, now = await _seeded(owner_engine)
    queries = {
        "orders": "SELECT unnest(ARRAY[created_at, submitted_at, quoted_at, confirmed_at, "
        "closed_at, cancelled_at]) FROM orders WHERE tenant_id = :t",
        "items": "SELECT created_at FROM order_items WHERE tenant_id = :t",
        "history": "SELECT created_at FROM order_status_history WHERE tenant_id = :t",
        "comments": "SELECT created_at FROM order_comments WHERE tenant_id = :t",
        "audit": "SELECT occurred_at FROM audit_events WHERE tenant_id = :t",
        "movements": "SELECT occurred_at FROM asset_movements WHERE tenant_id = :t",
        "assets": "SELECT created_at FROM assets WHERE tenant_id = :t",
        "customers": "SELECT created_at FROM customers WHERE tenant_id = :t",
        "products": "SELECT created_at FROM products WHERE tenant_id = :t",
        "users": "SELECT unnest(ARRAY[created_at, last_login_at]) FROM users WHERE tenant_id = :t",
        "contacts": "SELECT unnest(ARRAY[created_at, invited_at, accepted_at, last_login_at]) "
        "FROM customer_contacts WHERE tenant_id = :t",
    }
    async with owner_engine.connect() as conn:
        for name, sql in queries.items():
            stamps = [r[0] for r in await _rows(conn, sql, result.tenant_id) if r[0] is not None]
            assert stamps, name
            bad = [s.astimezone(PRAGUE) for s in stamps if not _in_business_hours(s) or s > now]
            assert not bad, (name, bad[:5])
        days = await _rows(
            conn,
            "SELECT unnest(ARRAY[requested_delivery_at, promised_delivery_at, delivered_at]) "
            "FROM orders WHERE tenant_id = :t",
            result.tenant_id,
        )
    assert not [d[0] for d in days if d[0] is not None and not demo_seed.is_working_day(d[0])]


@pytest.mark.postgres
async def test_comments_follow_the_status_they_describe(owner_engine, wipe_db) -> None:
    """P1-3: every comment is anchored to a status step and written after it."""
    result, _ = await _seeded(owner_engine)
    async with owner_engine.connect() as conn:
        for spec in demo_seed.ORDERS:
            notes = [e for e in spec.timeline if isinstance(e, demo_seed.Comment)]
            if not notes:
                continue
            rows = await _rows(
                conn,
                "SELECT c.created_at, c.body FROM order_comments c JOIN orders o "
                "ON o.id = c.order_id WHERE o.tenant_id = :t AND o.title = :title "
                "ORDER BY c.created_at",
                result.tenant_id,
                title=spec.title,
            )
            steps = dict(
                await _rows(
                    conn,
                    "SELECT h.to_status, h.created_at FROM order_status_history h "
                    "JOIN orders o ON o.id = h.order_id "
                    "WHERE o.tenant_id = :t AND o.title = :title",
                    result.tenant_id,
                    title=spec.title,
                )
            )
            assert len(rows) == len(notes), spec.title
            for note, row in zip(notes, rows, strict=True):
                assert row.created_at > steps[note.anchor.value], (spec.title, row.body[:50])
                assert "{" not in row.body, row.body  # every date placeholder rendered
                assert ".." not in row.body, row.body  # "do 7. 10.." — a date ends a sentence


@pytest.mark.postgres
async def test_overdue_order_is_realistic(owner_engine, wipe_db) -> None:
    """Exactly one overdue order: in production, promised 2–4 working days
    ago, apologised for within the last two working days."""
    result, now = await _seeded(owner_engine)
    today = now.astimezone(PRAGUE).date()
    async with owner_engine.connect() as conn:
        overdue = await _rows(
            conn,
            "SELECT id, title, status, promised_delivery_at FROM orders WHERE tenant_id = :t "
            "AND promised_delivery_at < :today "
            "AND status IN ('confirmed', 'in_production', 'ready')",
            result.tenant_id,
            today=today,
        )
        assert [r.title for r in overdue] == [demo_seed.OVERDUE_ORDER_TITLE]
        (row,) = overdue
        assert row.status == "in_production"
        late_by = sum(
            demo_seed.is_working_day(row.promised_delivery_at + timedelta(days=n))
            for n in range(1, (today - row.promised_delivery_at).days + 1)
        )
        assert 2 <= late_by <= 4
        latest = (
            await conn.execute(
                text(
                    "SELECT max(created_at) FROM order_comments "
                    "WHERE order_id = :o AND NOT is_internal"
                ),
                {"o": row.id},
            )
        ).scalar_one()
    recent = demo_seed.shift_working_days(demo_seed.anchor_day(now), -1)
    assert latest.astimezone(PRAGUE).date() >= recent


@pytest.mark.postgres
async def test_people_act_in_their_roles(owner_engine, wipe_db) -> None:
    """P2-8 / P2-11: every confirmation is the client's, the foreman runs the
    shop floor, whoever acted has accepted the invitation and signed in."""
    result, _ = await _seeded(owner_engine)
    tid = result.tenant_id
    async with owner_engine.connect() as conn:
        not_by_client = await _rows(
            conn,
            "SELECT o.number FROM orders o LEFT JOIN order_status_history h "
            "ON h.order_id = o.id AND h.to_status = 'confirmed' "
            "WHERE o.tenant_id = :t AND o.confirmed_at IS NOT NULL AND ("
            " o.confirmed_by_contact_id IS NULL OR h.changed_by_contact_id IS NULL"
            " OR h.changed_by_contact_id <> o.confirmed_by_contact_id)",
            tid,
        )
        confirmed = await _rows(
            conn,
            "SELECT count(*) FROM orders WHERE tenant_id = :t AND confirmed_at IS NOT NULL",
            tid,
        )
        floor = await _rows(
            conn,
            "SELECT DISTINCT u.email FROM order_status_history h JOIN users u "
            "ON u.id = h.changed_by_user_id WHERE h.tenant_id = :t "
            "AND h.to_status IN ('in_production', 'ready')",
            tid,
        )
        floor_by_contact = await _rows(
            conn,
            "SELECT count(*) FROM order_status_history WHERE tenant_id = :t "
            "AND to_status IN ('quoted', 'in_production', 'ready', 'delivered', 'closed') "
            "AND changed_by_contact_id IS NOT NULL",
            tid,
        )
        silent = await _rows(
            conn,
            "SELECT c.full_name FROM customer_contacts c WHERE c.tenant_id = :t AND ("
            " EXISTS (SELECT 1 FROM order_status_history h WHERE h.changed_by_contact_id = c.id)"
            " OR EXISTS (SELECT 1 FROM order_comments m WHERE m.author_contact_id = c.id)"
            " OR EXISTS (SELECT 1 FROM orders o WHERE o.created_by_contact_id = c.id))"
            " AND (c.accepted_at IS NULL OR c.last_login_at IS NULL OR c.password_hash IS NULL)",
            tid,
        )
        pending = await _rows(
            conn,
            "SELECT full_name FROM customer_contacts WHERE tenant_id = :t AND accepted_at IS NULL",
            tid,
        )
    assert confirmed[0][0] >= 10
    assert not not_by_client
    assert [r.email for r in floor] == ["dilna@dilna-vzorova.example.com"]
    assert floor_by_contact[0][0] == 0
    assert not silent
    assert 1 <= len(pending) <= 2


@pytest.mark.postgres
async def test_sla_and_quotes_are_believable(owner_engine, wipe_db) -> None:
    """P2-9 / P2-10 / P3-4: a late delivery or two, history matches
    ``delivered_at``, every quote has a promised date, READY is not overdue."""
    from app.services import sla_service

    result, now = await _seeded(owner_engine)
    today = now.astimezone(PRAGUE).date()
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        sla = await sla_service.on_time_rate(
            session, date_from=today - timedelta(days=90), date_to=today, today=today
        )
    # The owner role sees every tenant; nothing else is seeded in this test.
    assert 1 <= sla["late"] <= 2
    assert sla["on_time"] >= 4
    assert sla["pending"] == 1  # the overdue order — nothing READY counts
    tid = result.tenant_id
    async with owner_engine.connect() as conn:
        mismatch = await _rows(
            conn,
            "SELECT o.number FROM orders o JOIN order_status_history h ON h.order_id = o.id "
            "AND h.to_status = 'delivered' WHERE o.tenant_id = :t "
            "AND (h.created_at AT TIME ZONE 'Europe/Prague')::date <> o.delivered_at",
            tid,
        )
        no_promise = await _rows(
            conn,
            "SELECT number FROM orders WHERE tenant_id = :t AND promised_delivery_at IS NULL "
            "AND status IN ('quoted', 'confirmed', 'in_production', 'ready', 'delivered', "
            "'closed')",
            tid,
        )
    assert not mismatch
    assert not no_promise


@pytest.mark.postgres
async def test_prices_and_finishes_are_plausible(owner_engine, wipe_db) -> None:
    """P1-5 / P3-3 / P3-5: no double finishing charge, zinc weight matches
    the parts, Czech job-shop rates, hour lines say how many parts."""
    result, _ = await _seeded(owner_engine)
    async with owner_engine.connect() as conn:
        items = await _rows(
            conn,
            "SELECT o.title, i.description, i.quantity, i.unit, i.unit_price "
            "FROM order_items i JOIN orders o ON o.id = i.order_id WHERE o.tenant_id = :t",
            result.tenant_id,
        )
        totals = await _rows(
            conn,
            "SELECT title, coalesce(confirmed_total, quoted_total) AS total FROM orders "
            "WHERE tenant_id = :t AND status NOT IN ('draft', 'cancelled')",
            result.tenant_id,
        )
    by_order: dict[str, list] = {}
    for row in items:
        by_order.setdefault(row.title, []).append(row)
    for title, rows in by_order.items():
        finish_lines = [r for r in rows if r.description.startswith(FINISH_LINES)]
        finished_items = [
            r
            for r in rows
            if r not in finish_lines and any(w in r.description.lower() for w in FINISH_WORDS)
        ]
        # A finish is in the item's name or on its own line — never both.
        assert not (finish_lines and finished_items), (title, finished_items, finish_lines)
        for r in rows:
            if r.description.startswith("Žárové zinkování"):
                parts = sum(x.quantity for x in rows if x.unit == "ks")
                assert Decimal("0.3") <= r.quantity / parts <= Decimal("1.5"), title
            if r.unit == "hod":
                assert Decimal(650) <= r.unit_price <= Decimal(1100), (title, r.description)
                assert "ks" in r.description, (title, r.description)  # for how many parts
    for row in totals:
        assert Decimal(2000) <= row.total <= Decimal(300_000), (row.title, row.total)
    small_parts = [r for r in items if r.description.startswith("Konzole motoru KM-120")]
    assert small_parts and all(r.unit_price < 90 for r in small_parts)  # was 385 Kč/ks


@pytest.mark.postgres
async def test_showcase_lookup_and_material(owner_engine, wipe_db) -> None:
    """The "where to start" targets resolve by query and look the part;
    material movements belong to the orders that used the material."""
    result, _ = await _seeded(owner_engine)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        show = await demo_seed.find_showcase(session, result.tenant_id)
        nothing = await demo_seed.find_showcase(session, uuid4())
    assert nothing == demo_seed.DemoShowcase(None, None, None)
    assert show.flagship_order and show.overdue_order and show.material
    assert show.material.label == demo_seed.SHOWCASE_MATERIAL_CODE
    tid = result.tenant_id
    async with owner_engine.connect() as conn:
        flagship = (
            await conn.execute(
                text("SELECT * FROM orders WHERE id = :o"), {"o": show.flagship_order.id}
            )
        ).one()
        (newest_quote,) = (
            await _rows(
                conn,
                "SELECT id FROM orders WHERE tenant_id = :t AND status = 'quoted' "
                "ORDER BY quoted_at DESC LIMIT 1",
                tid,
            )
        )[0]
        moves = await _rows(
            conn,
            "SELECT a.code, m.type, m.occurred_at, o.created_at AS order_created, o.status, "
            "(SELECT h.created_at FROM order_status_history h WHERE h.order_id = o.id "
            " AND h.to_status = 'in_production') AS production_at "
            "FROM asset_movements m JOIN assets a ON a.id = m.asset_id "
            "LEFT JOIN orders o ON o.id = m.reference_order_id WHERE a.tenant_id = :t",
            tid,
        )
    assert flagship.number == show.flagship_order.label
    assert flagship.status == "quoted" and flagship.id == newest_quote
    assert flagship.promised_delivery_at is not None
    assert flagship.requested_delivery_at >= flagship.promised_delivery_at
    assert Decimal(40_000) <= flagship.quoted_total <= Decimal(60_000)
    per_asset: dict[str, int] = {}
    for m in moves:
        per_asset[m.code] = per_asset.get(m.code, 0) + 1
        if m.order_created is not None:
            assert m.occurred_at > m.order_created, m
        if m.type == "consume":
            assert m.production_at is not None and m.occurred_at > m.production_at, m
    assert all(3 <= n <= 5 for n in per_asset.values()), per_asset
    assert sum(m.order_created is not None for m in moves) >= 12
    assert any(m.type == "consume" and m.status == "in_production" for m in moves)


# ------------------------------------------------------- round 2 (N1-N9)


def test_next_working_day_is_said_the_way_people_say_it() -> None:
    """N4: "zítra" only when the next working day is tomorrow."""
    phrase = demo_seed.next_working_day_phrase
    assert phrase(date(2026, 10, 5)) == "zítra"  # Monday
    assert phrase(date(2026, 10, 2)) == "v pondělí"  # Friday
    assert phrase(date(2026, 9, 25)) == "v úterý"  # Friday before St Wenceslas (Mon 28. 9.)
    assert phrase(date(2026, 10, 27)) == "ve čtvrtek"  # Tuesday before 28. 10. (Wed)
    assert phrase(date(2026, 4, 2)) == "v úterý"  # Thursday before Easter


def test_persona_has_three_open_quotes_with_drawings() -> None:
    """N1: the shared demo's customer persona has more than one quote to confirm."""
    quotes = demo_seed.persona_quote_templates()
    assert len(quotes) == 3
    assert quotes[0].title == demo_seed.FLAGSHIP_ORDER_TITLE
    for spec in quotes:
        assert spec.client == demo_seed.PERSONA_CLIENT
        assert any(isinstance(e, demo_seed.Upload) for e in spec.timeline), spec.title
        assert spec.promised is not None and spec.requested is not None
    # The flagship is the newest quote, so the persona lands on it first.
    quoted_wd = {
        s.title: next(e.wd for e in s.steps if e.status == OrderStatus.QUOTED) for s in quotes
    }
    assert min(quoted_wd, key=lambda t: quoted_wd[t]) == demo_seed.FLAGSHIP_ORDER_TITLE
    created_wd = {s.title: s.timeline[0].wd for s in quotes}
    assert min(created_wd, key=lambda t: created_wd[t]) == demo_seed.FLAGSHIP_ORDER_TITLE


@pytest.mark.parametrize(
    "now",
    [
        _prague(2026, 10, 6, 2, 30),  # Tuesday: the anchor is Monday
        _prague(2026, 10, 5, 2, 30),  # Monday: the anchor is Friday
        _prague(2026, 9, 29, 2, 30),  # after a public holiday
    ],
)
def test_comment_dates_agree_with_the_calendar(now: datetime) -> None:
    """N4 / N9: relative words and confirmation deadlines match the dates.

    No "zítra" said on a Friday; an open quote's comments only name dates
    after the anchor day (a deadline that has already passed would make
    the quote look stale); no "within N working days" left to arithmetic.
    """
    clock = demo_seed.DemoClock(now)
    for p in demo_seed.plan_orders(clock):
        for event, at in zip(p.spec.timeline, p.times, strict=True):
            if not isinstance(event, demo_seed.Comment):
                continue
            said_on = at.astimezone(PRAGUE).date()
            body = demo_seed._render_text(event.body, p, clock, said_on=said_on)
            if "zítra" in body:
                assert demo_seed.shift_working_days(said_on, 1) == said_on + timedelta(days=1)
            assert not re.search(r"do \d+ pracovních dnů", body), body
            if p.spec.status == OrderStatus.QUOTED:
                for d, m in re.findall(r"\b(\d{1,2})\. (\d{1,2})\.", body):
                    year = clock.anchor.year + (int(m) < clock.anchor.month - 6)
                    assert date(year, int(m), int(d)) > clock.anchor, (p.spec.title, body)


@pytest.mark.postgres
async def test_last_working_day_is_busy(owner_engine, wipe_db) -> None:
    """N2: on the anchor day a quote went out, an order became ready, a new
    enquiry arrived and people commented — "Recent activity" is not idle."""
    result, now = await _seeded(owner_engine)
    anchor = demo_seed.anchor_day(now)
    async with owner_engine.connect() as conn:
        rows = await _rows(
            conn,
            "SELECT action, diff FROM audit_events WHERE tenant_id = :t "
            "AND (occurred_at AT TIME ZONE 'Europe/Prague')::date = :d",
            result.tenant_id,
            d=anchor,
        )
        tenant_settings = (
            await conn.execute(
                text("SELECT settings FROM tenants WHERE id = :t"), {"t": result.tenant_id}
            )
        ).scalar_one()
    statuses = {r.diff["after"]["status"] for r in rows if r.action == "order.status_changed"}
    assert {"quoted", "ready", "submitted"} <= statuses
    assert sum(r.action == "order.comment_added" for r in rows) >= 3
    # P2-3: the demo shop's PDFs say what the prices are.
    assert tenant_settings["price_note"] == demo_seed.DEMO_PRICE_NOTE
