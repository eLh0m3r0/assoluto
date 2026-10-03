"""E-mail reliability: durable outbox (BE-09) and connection release (BE-08)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from app.email import outbox
from app.email.sender import CaptureSender
from app.tasks import email_tasks
from app.tasks.email_tasks import send_invitation

pytestmark = pytest.mark.postgres


class ExplodingSender:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, **kwargs) -> None:
        self.calls += 1
        raise ConnectionError("smtp relay unreachable")


def _invite(sender, to: str = "jan@acme.cz") -> None:
    send_invitation(
        sender,
        to=to,
        tenant_name="4MEX",
        customer_name="ACME",
        contact_name="Jan",
        invite_url="https://4mex.example/invite/accept?token=secret-token-value",
    )


async def _rows(owner_engine) -> list:
    async with owner_engine.connect() as conn:
        return list(
            (
                await conn.execute(
                    text(
                        "SELECT to_address, template, context, attempts, sent_at, failed_at, "
                        "next_attempt_at, last_error FROM email_outbox ORDER BY created_at"
                    )
                )
            ).all()
        )


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    monkeypatch.setattr(email_tasks, "_BACKOFF_BASE_SECONDS", 0)


async def test_successful_send_is_recorded_and_context_wiped(owner_engine, wipe_db) -> None:
    capture = CaptureSender()
    _invite(capture)

    assert len(capture.outbox) == 1
    rows = await _rows(owner_engine)
    assert len(rows) == 1
    row = rows[0]
    assert row.to_address == "jan@acme.cz"
    assert row.template == "invitation"
    assert row.sent_at is not None
    assert row.attempts == 1
    # The one-shot invitation URL must not linger in the table.
    assert row.context is None


async def test_failed_send_survives_and_is_retried_by_the_job(owner_engine, wipe_db) -> None:
    broken = ExplodingSender()
    _invite(broken)  # must not raise

    assert broken.calls == email_tasks._MAX_ATTEMPTS
    [row] = await _rows(owner_engine)
    assert row.sent_at is None
    assert row.failed_at is None
    assert row.attempts == email_tasks._MAX_ATTEMPTS
    assert "ConnectionError" in row.last_error
    assert row.context is not None  # kept so the retry can render it

    # Not due yet: the job leaves it alone.
    capture = CaptureSender()
    stats = await outbox.drain_outbox_now(capture)
    assert stats["sent"] == 0
    assert capture.outbox == []

    # Once the backoff has elapsed the job delivers it.
    later = datetime.now(UTC) + timedelta(hours=1)
    stats = await outbox.drain_outbox_now(capture, now=later)
    assert stats["sent"] == 1
    assert [m.to for m in capture.outbox] == ["jan@acme.cz"]
    assert "secret-token-value" in capture.outbox[0].html
    [row] = await _rows(owner_engine)
    assert row.sent_at is not None
    assert row.context is None


async def test_job_gives_up_after_max_attempts(owner_engine, wipe_db) -> None:
    broken = ExplodingSender()
    _invite(broken)

    now = datetime.now(UTC)
    for _ in range(outbox.MAX_ATTEMPTS):
        now += outbox.BACKOFF_CAP + timedelta(minutes=1)
        await outbox.drain_outbox_now(broken, now=now)

    [row] = await _rows(owner_engine)
    assert row.sent_at is None
    assert row.failed_at is not None
    assert row.attempts == outbox.MAX_ATTEMPTS
    calls_at_give_up = broken.calls

    # A given-up row is never tried again.
    await outbox.drain_outbox_now(broken, now=now + timedelta(days=1))
    assert broken.calls == calls_at_give_up


async def test_backoff_grows_and_is_capped() -> None:
    assert outbox.backoff_after(1) == timedelta(minutes=1)
    assert outbox.backoff_after(3) == timedelta(minutes=4)
    assert outbox.backoff_after(4) == timedelta(minutes=8)
    assert outbox.backoff_after(50) == outbox.BACKOFF_CAP


async def test_locked_row_is_skipped_not_double_sent(owner_engine, wipe_db) -> None:
    _invite(ExplodingSender())
    later = datetime.now(UTC) + timedelta(hours=1)

    capture = CaptureSender()
    async with owner_engine.connect() as locker:
        tx = await locker.begin()
        await locker.execute(text("SELECT id FROM email_outbox FOR UPDATE"))
        # Another worker holds the row: this run must skip it, not wait or send.
        stats = await outbox.drain_outbox_now(capture, now=later)
        assert stats["sent"] == 0
        await tx.rollback()

    stats = await outbox.drain_outbox_now(capture, now=later)
    assert stats["sent"] == 1
    assert len(capture.outbox) == 1


async def test_outbox_unavailable_falls_back_to_direct_send(monkeypatch, wipe_db) -> None:
    def broken_enqueue(**kwargs):
        raise OSError("database is down")

    monkeypatch.setattr(outbox, "enqueue", broken_enqueue)
    capture = CaptureSender()
    _invite(capture)
    assert len(capture.outbox) == 1


async def test_killswitch_sends_and_stores_nothing(settings, owner_engine, wipe_db) -> None:
    settings.enable_outbound_emails = False
    capture = CaptureSender()
    _invite(capture)
    assert capture.outbox == []
    assert await _rows(owner_engine) == []


async def test_old_sent_rows_are_purged(owner_engine, wipe_db) -> None:
    _invite(CaptureSender())
    stats = await outbox.drain_outbox_now(
        CaptureSender(), now=datetime.now(UTC) + outbox.SENT_RETENTION + timedelta(days=1)
    )
    assert stats["purged"] == 1
    assert await _rows(owner_engine) == []


async def test_order_notification_context_round_trips(owner_engine, wipe_db) -> None:
    """Order payloads (incl. the digest's list of dicts) survive JSON storage."""
    from app.services.notification_prefs import NotificationEvent
    from app.services.notification_service import OrderDigestNotification, Recipient

    digest = OrderDigestNotification(
        event=NotificationEvent.ORDER_STATUS_CHANGED,
        recipient=Recipient(email="eva@acme.cz", locale="cs", full_name="Eva"),
        tenant_name="4MEX",
        orders=[
            {"number": "2026-1", "title": "A", "url": "https://x/1"},
            {"number": "2026-2", "title": "B", "url": "https://x/2"},
        ],
    )
    broken = ExplodingSender()
    email_tasks.send_order_notifications(broken, [digest])

    capture = CaptureSender()
    await outbox.drain_outbox_now(capture, now=datetime.now(UTC) + timedelta(hours=1))
    assert len(capture.outbox) == 1
    assert "2026-2" in capture.outbox[0].html


# ------------------------------------------------------------------- BE-08


async def test_password_reset_releases_db_connection_before_smtp(
    tenant_client: AsyncClient, owner_engine, demo_tenant, monkeypatch
) -> None:
    """The background send must not run while the request still holds a
    pooled connection 'idle in transaction' (BE-08)."""
    from app.db.session import get_engine
    from tests.test_notifications_flow import _seed

    await _seed(owner_engine, demo_tenant.id)
    checked_out: list[int] = []

    def spy(sender, **kwargs):
        checked_out.append(get_engine().pool.checkedout())

    monkeypatch.setattr(email_tasks, "send_password_reset", spy)
    resp = await tenant_client.post("/auth/password-reset", data={"email": "owner@4mex.cz"})
    assert resp.status_code == 200
    assert checked_out == [0]
