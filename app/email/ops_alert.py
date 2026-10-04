"""Operator e-mail alert for unhandled errors (audit BE-05 / F-36).

There is no external error tracker. A real production 500 on
2026-09-08 sat unnoticed in the container log for weeks, so the app now
mails the operator directly when something blows up:

* **Opt-in** — ``OPS_ALERT_EMAIL`` empty (the default) disables it.
* **Rate-limited in process** — at most one mail per error *signature*
  (exception type + where it was raised + route) per
  :data:`ALERT_INTERVAL`, so a crash loop sends one mail, not thousands.
  Suppressed repeats are counted and reported in the next mail.
* **No request data** — path, method, exception type, request id and the
  traceback *frames* (file, line, function). Never the request body,
  headers, cookies, query string or local variables. The exception
  message is passed through the same redaction as SMTP errors (URLs,
  tokens, long hex) and truncated, because messages such as
  ``Key (email)=(…)`` can carry data.
* **Never blocks or raises** — the SMTP send runs in a worker thread as
  a detached task; any failure is logged and dropped.
"""

from __future__ import annotations

import asyncio
import html
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio

from app.logging import get_logger

log = get_logger("app.ops_alert")

ALERT_INTERVAL = timedelta(minutes=15)
_TRACEBACK_FRAMES = 12

_lock = threading.Lock()
# signature -> (last_sent_monotonic, suppressed_since_then)
_last_sent: dict[str, tuple[float, int]] = {}
# Strong refs so detached send tasks aren't garbage-collected mid-flight;
# tests await them via :func:`wait_for_pending_alerts`.
_pending: set[asyncio.Task[Any]] = set()


@dataclass(frozen=True)
class AlertDecision:
    send: bool
    suppressed: int = 0


def _signature(exc: BaseException, where: str) -> str:
    tb = traceback.extract_tb(exc.__traceback__)
    origin = f"{tb[-1].filename}:{tb[-1].lineno}" if tb else "?"
    return f"{type(exc).__name__}|{origin}|{where}"


def _should_send(signature: str, now: float | None = None) -> AlertDecision:
    current = time.monotonic() if now is None else now
    interval = ALERT_INTERVAL.total_seconds()
    with _lock:
        previous = _last_sent.get(signature)
        if previous is not None and current - previous[0] < interval:
            _last_sent[signature] = (previous[0], previous[1] + 1)
            return AlertDecision(send=False)
        suppressed = previous[1] if previous is not None else 0
        _last_sent[signature] = (current, 0)
        return AlertDecision(send=True, suppressed=suppressed)


def reset_for_tests() -> None:
    with _lock:
        _last_sent.clear()


def _frames(exc: BaseException) -> list[str]:
    """File/line/function of the innermost frames — no source, no locals."""
    tb = traceback.extract_tb(exc.__traceback__)[-_TRACEBACK_FRAMES:]
    return [f'  File "{f.filename}", line {f.lineno}, in {f.name}' for f in tb]


def build_alert(
    exc: BaseException,
    *,
    where: str,
    request_id: str | None,
    method: str | None,
    environment: str,
    suppressed: int = 0,
) -> tuple[str, str]:
    """Return ``(subject, text)`` for one alert."""
    from app.tasks.email_tasks import _safe_error_summary

    error_type = type(exc).__name__
    message = _safe_error_summary(exc) if isinstance(exc, Exception) else ""
    lines = [
        f"Unhandled {error_type} in Assoluto ({environment}).",
        "",
        f"When:       {datetime.now(UTC).isoformat()}",
        f"Where:      {method + ' ' if method else ''}{where}",
        f"Request id: {request_id or '-'}",
        f"Error:      {error_type}: {message}",
    ]
    if suppressed:
        lines.append(
            f"Repeats:    {suppressed} identical error(s) suppressed since the previous alert"
        )
    lines += [
        "",
        "Traceback (innermost frames):",
        *_frames(exc),
        "",
        "Search the container log for the request id for full context:",
        "  docker compose logs web | grep <request id>",
        "",
        f"Identical errors are mailed at most once per "
        f"{int(ALERT_INTERVAL.total_seconds() // 60)} minutes.",
    ]
    subject = f"[Assoluto {environment}] {error_type} at {where}"[:200]
    return subject, "\n".join(lines)


def notify_unhandled(
    exc: BaseException,
    *,
    settings: Any,
    sender: Any,
    where: str,
    request_id: str | None = None,
    method: str | None = None,
) -> bool:
    """Schedule an operator alert for ``exc``; return True if one was queued.

    Safe to call from any ``async`` context (exception handlers,
    scheduler listeners). Never raises.
    """
    try:
        to = (getattr(settings, "ops_alert_email", "") or "").strip()
        if not to or sender is None:
            return False
        if not getattr(settings, "enable_outbound_emails", True):
            return False
        decision = _should_send(_signature(exc, where))
        if not decision.send:
            return False
        subject, text = build_alert(
            exc,
            where=where,
            request_id=request_id,
            method=method,
            environment=getattr(settings, "app_env", "?"),
            suppressed=decision.suppressed,
        )
        body_html = f"<pre>{html.escape(text)}</pre>"

        def _send() -> None:
            try:
                sender.send(to=to, subject=subject, html=body_html, text=text)
                log.info("ops_alert.sent", where=where)
            except Exception as send_exc:
                log.warning("ops_alert.send_failed", error_class=type(send_exc).__name__)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _send()
            return True
        task = loop.create_task(anyio.to_thread.run_sync(_send))
        _pending.add(task)
        task.add_done_callback(_pending.discard)
        return True
    except Exception as alert_exc:  # pragma: no cover - last-resort guard
        log.warning("ops_alert.failed", error_class=type(alert_exc).__name__)
        return False


async def wait_for_pending_alerts() -> None:
    """Await every in-flight alert send (tests)."""
    while _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)
