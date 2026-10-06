"""Durable e-mail outbox (audit BE-09).

Before this existed, a templated mail lived only in a request's
``BackgroundTasks`` list: three SMTP attempts within ~6 s and then it
was gone, and a deploy (SIGTERM) dropped whatever was still queued. A
Brevo blip longer than a few seconds silently lost password resets and
invitations.

Now every templated mail is first written to ``email_outbox`` and only
then sent:

1. **Inline attempt** — :func:`deliver_via_outbox` (called from the
   existing ``send_*`` helpers in :mod:`app.tasks.email_tasks`, so no
   caller changed) inserts the row, claims it and tries to send right
   away with the same quick retries as before. The happy path is
   exactly as fast as it used to be, and tests that capture mail keep
   seeing it immediately.
2. **Retry job** — rows that are still unsent are picked up by
   :func:`deliver_email_outbox` (scheduler, every minute) with
   exponential backoff (4 min, 8 min, … capped at 4 h) until
   :data:`MAX_ATTEMPTS` is reached (~16 h of trying). Then the row is
   marked ``failed_at`` and an ``email.outbox_gave_up`` error is logged.

Concurrency: every send path claims its row with
``SELECT … FOR UPDATE SKIP LOCKED`` inside the transaction that later
records the outcome, so the inline sender and the job (or two workers)
can never send the same row twice. A freshly inserted row is also
invisible to the job for :data:`INLINE_GRACE` so the inline attempt has
first go.

Privacy: the table is owner-only (``portal_app`` has no grant), the
rendering context (which may carry a one-shot reset or invitation URL)
is wiped as soon as the mail is sent, and sent / failed rows are purged
after 7 / 30 days.

If the outbox itself is unavailable (DB down), the helpers fall back to
the old direct send, so an outbox problem never costs a mail that SMTP
could have delivered.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any
from uuid import UUID, uuid4

import anyio
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from app.logging import get_logger

log = get_logger("app.email.outbox")

#: Total send attempts (inline retries included) before giving up.
MAX_ATTEMPTS = 12
BACKOFF_BASE = timedelta(minutes=1)
BACKOFF_CAP = timedelta(hours=4)
#: The retry job leaves a new row alone this long so the inline attempt
#: (which may be mid-retry) gets the first go.
INLINE_GRACE = timedelta(minutes=2)
SENT_RETENTION = timedelta(days=7)
FAILED_RETENTION = timedelta(days=30)
#: Rows processed per job run — bounds one run's duration.
JOB_BATCH = 100

_COLUMNS = "id, tenant_id, kind, template, to_address, context, locale, attempts"


def backoff_after(attempts: int) -> timedelta:
    """Delay before the next try after ``attempts`` failed sends."""
    exponent = min(max(0, attempts - 1), 20)
    delay = BACKOFF_BASE * (2**exponent)
    return min(delay, BACKOFF_CAP)


@lru_cache(maxsize=4)
def _engine_for(url: str) -> Engine:
    # Small dedicated pool on the owner DSN: the outbox is owner-only,
    # and send attempts must never compete with request traffic for the
    # app's asyncpg pool.
    return create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=3)


def _sync_engine() -> Engine:
    from app.config import get_settings

    return _engine_for(get_settings().database_sync_url)


def _json_context(context: dict[str, Any]) -> str:
    return json.dumps(context, default=str, ensure_ascii=False)


def _short_error(exc: BaseException | None) -> str:
    if exc is None:
        return ""
    from app.tasks.email_tasks import _safe_error_summary

    summary = _safe_error_summary(exc) if isinstance(exc, Exception) else ""
    return f"{type(exc).__name__}: {summary}"[:255]


def _render(row: Any) -> tuple[str, str, str]:
    from app.email.sender import render_email

    context = row.context
    if isinstance(context, str):
        context = json.loads(context)
    rendered = render_email(row.template, context or {}, locale=row.locale)
    return rendered.subject, rendered.html, rendered.text


def _attempt(sender: Any, row: Any, max_tries: int) -> tuple[bool, int, BaseException | None]:
    """Render ``row`` and send it with up to ``max_tries`` quick retries.

    Returns ``(sent, tries_used, last_error)``. Blocking.
    """
    from app.tasks.email_tasks import _send_with_retries

    try:
        subject, html, body_text = _render(row)
    except Exception as exc:
        log.error(
            "email.render_failed",
            kind=row.kind,
            error_class=type(exc).__name__,
        )
        return False, 1, exc
    error, tries = _send_with_retries(
        sender, row.kind, row.to_address, subject, html, body_text, max_tries
    )
    return error is None, tries, error


def _record_outcome(
    conn: Connection,
    row: Any,
    *,
    sent: bool,
    tries: int,
    error: BaseException | None,
    now: datetime,
) -> None:
    attempts = (row.attempts or 0) + tries
    if sent:
        conn.execute(
            text(
                "UPDATE email_outbox SET sent_at = :now, attempts = :attempts, "
                "context = NULL, last_error = NULL WHERE id = :id"
            ),
            {"now": now, "attempts": attempts, "id": row.id},
        )
        return
    if attempts >= MAX_ATTEMPTS:
        conn.execute(
            text(
                "UPDATE email_outbox SET failed_at = :now, attempts = :attempts, "
                "last_error = :err WHERE id = :id"
            ),
            {"now": now, "attempts": attempts, "err": _short_error(error), "id": row.id},
        )
        log.error(
            "email.outbox_gave_up",
            kind=row.kind,
            to=row.to_address,
            attempts=attempts,
            error=_short_error(error),
        )
        return
    conn.execute(
        text(
            "UPDATE email_outbox SET attempts = :attempts, last_error = :err, "
            "next_attempt_at = :next WHERE id = :id"
        ),
        {
            "attempts": attempts,
            "err": _short_error(error),
            "next": now + backoff_after(attempts),
            "id": row.id,
        },
    )
    log.warning(
        "email.outbox_deferred",
        kind=row.kind,
        to=row.to_address,
        attempts=attempts,
        retry_in_s=int(backoff_after(attempts).total_seconds()),
    )


def _drop_if_public_demo(conn: Connection, row: Any, now: datetime) -> bool:
    """Retire a queued mail of the public demo tenant instead of sending it.

    Mail is suppressed before it is enqueued (``app.tasks.email_tasks``);
    this catches rows queued before ``PUBLIC_DEMO_TENANT`` was switched
    on. The context (which may hold a one-shot link) is wiped.
    """
    from app.demo.guard import mail_suppressed

    if row.tenant_id is None or not mail_suppressed(row.tenant_id):
        return False
    conn.execute(
        text(
            "UPDATE email_outbox SET failed_at = :now, context = NULL, "
            "last_error = 'suppressed: public demo tenant' WHERE id = :id"
        ),
        {"now": now, "id": row.id},
    )
    log.info("email.suppressed_public_demo", kind=row.kind, to=row.to_address, outbox=True)
    return True


# ------------------------------------------------------------------ enqueue


def enqueue(
    *,
    kind: str,
    template: str,
    to: str,
    context: dict[str, Any],
    locale: str | None,
    tenant_id: UUID | None = None,
    now: datetime | None = None,
    engine: Engine | None = None,
) -> UUID:
    """Insert one outbox row (own transaction) and return its id. Blocking."""
    current = now or datetime.now(UTC)
    row_id = uuid4()
    with (engine or _sync_engine()).begin() as conn:
        conn.execute(
            text(
                "INSERT INTO email_outbox (id, tenant_id, kind, template, to_address, "
                "context, locale, attempts, next_attempt_at, created_at) VALUES "
                "(:id, :tenant_id, :kind, :template, :to, CAST(:context AS JSONB), "
                ":locale, 0, :next, :now)"
            ),
            {
                "id": row_id,
                "tenant_id": tenant_id,
                "kind": kind[:64],
                "template": template[:64],
                "to": to[:320],
                "context": _json_context(context),
                "locale": locale,
                "next": current + INLINE_GRACE,
                "now": current,
            },
        )
    return row_id


def deliver_row(sender: Any, row_id: UUID, *, max_tries: int, engine: Engine | None = None) -> bool:
    """Claim one row and try to send it now. Blocking. Returns True if sent."""
    with (engine or _sync_engine()).begin() as conn:
        row = conn.execute(
            text(
                f"SELECT {_COLUMNS} FROM email_outbox "
                "WHERE id = :id AND sent_at IS NULL AND failed_at IS NULL "
                "FOR UPDATE SKIP LOCKED"
            ),
            {"id": row_id},
        ).first()
        if row is None:
            return False  # someone else has it, or it is already done
        if _drop_if_public_demo(conn, row, datetime.now(UTC)):
            return False
        sent, tries, error = _attempt(sender, row, max_tries)
        _record_outcome(conn, row, sent=sent, tries=tries, error=error, now=datetime.now(UTC))
        return sent


def deliver_via_outbox(
    sender: Any,
    *,
    kind: str,
    template: str,
    to: str,
    context: dict[str, Any],
    locale: str | None,
    max_tries: int,
    tenant_id: UUID | None = None,
) -> bool:
    """Persist the mail, then attempt it inline. Blocking.

    Returns ``False`` only when the outbox could not be written — the
    caller then falls back to a direct send.
    """
    try:
        row_id = enqueue(
            kind=kind,
            template=template,
            to=to,
            context=context,
            locale=locale,
            tenant_id=tenant_id,
        )
    except Exception as exc:
        log.warning("email.outbox_unavailable", kind=kind, error_class=type(exc).__name__)
        return False
    try:
        deliver_row(sender, row_id, max_tries=max_tries)
    except Exception as exc:
        # The row is safely stored; the retry job will pick it up.
        log.warning("email.outbox_inline_failed", kind=kind, error_class=type(exc).__name__)
    return True


# --------------------------------------------------------------------- job


def _drain_blocking(sender: Any, now: datetime, limit: int, engine: Engine) -> dict[str, int]:
    stats = {"sent": 0, "deferred": 0, "purged": 0}
    for _ in range(limit):
        with engine.begin() as conn:
            row = conn.execute(
                text(
                    f"SELECT {_COLUMNS} FROM email_outbox "
                    "WHERE sent_at IS NULL AND failed_at IS NULL "
                    "AND next_attempt_at <= :now "
                    "ORDER BY next_attempt_at LIMIT 1 FOR UPDATE SKIP LOCKED"
                ),
                {"now": now},
            ).first()
            if row is None:
                break
            if _drop_if_public_demo(conn, row, now):
                continue
            # One try per job run; the backoff spaces them out.
            sent, tries, error = _attempt(sender, row, 1)
            _record_outcome(conn, row, sent=sent, tries=tries, error=error, now=now)
            stats["sent" if sent else "deferred"] += 1

    with engine.begin() as conn:
        purged = conn.execute(
            text(
                "DELETE FROM email_outbox WHERE "
                "(sent_at IS NOT NULL AND sent_at < :sent_cutoff) OR "
                "(failed_at IS NOT NULL AND failed_at < :failed_cutoff)"
            ),
            {"sent_cutoff": now - SENT_RETENTION, "failed_cutoff": now - FAILED_RETENTION},
        )
        stats["purged"] = purged.rowcount or 0
    return stats


async def deliver_email_outbox(
    now: datetime | None = None,
    sender: Any = None,
    limit: int = JOB_BATCH,
) -> dict[str, int]:
    """Scheduler job: send due outbox rows, purge old ones.

    Everything (DB + SMTP) runs in one worker thread, so the event loop
    is never blocked. Safe with several workers: rows are claimed with
    ``FOR UPDATE SKIP LOCKED``.
    """
    from app.config import get_settings
    from app.email.sender import build_sender

    settings = get_settings()
    if not settings.enable_outbound_emails:
        log.info("email.outbox_paused", reason="ENABLE_OUTBOUND_EMAILS=false")
        return {"sent": 0, "deferred": 0, "purged": 0}
    mail = sender if sender is not None else build_sender(settings)
    current = now or datetime.now(UTC)
    stats = await anyio.to_thread.run_sync(_drain_blocking, mail, current, limit, _sync_engine())
    if stats["sent"] or stats["deferred"] or stats["purged"]:
        log.info("email.outbox_drained", **stats)
    return stats


async def drain_outbox_now(sender: Any, now: datetime | None = None) -> dict[str, int]:
    """Test/ops helper: run the retry job synchronously with ``sender``.

    Pass a ``now`` in the future to make backed-off rows due, e.g.
    ``now=datetime.now(UTC) + timedelta(hours=1)``.
    """
    return await deliver_email_outbox(now=now, sender=sender)
