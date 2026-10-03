"""Operator alerting and readiness (T10 — audit BE-05 / F-36)."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.email import ops_alert
from app.email.sender import CaptureSender
from app.main import create_app
from app.routers import health


@pytest.fixture(autouse=True)
def _reset_alert_state():
    ops_alert.reset_for_tests()
    health.reset_s3_probe_cache()
    yield
    ops_alert.reset_for_tests()
    health.reset_s3_probe_cache()


def _app_with_boom(settings):
    app = create_app(settings)
    capture = CaptureSender()
    app.state.email_sender = capture

    async def boom(order_id: str):
        raise RuntimeError("kaboom token=abcdefghijklmnopqrstuvwxyz")

    app.add_api_route("/boom/{order_id}", boom, methods=["GET", "POST"])
    return app, capture


async def test_unhandled_500_mails_the_operator_once_per_signature(settings) -> None:
    settings.ops_alert_email = "ops@example.com"
    app, capture = _app_with_boom(settings)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.get(
            "/boom/123",
            headers={"x-request-id": "req-abc123", "cookie": "portal_session=SECRETCOOKIE"},
        )
        assert resp.status_code == 500
        await ops_alert.wait_for_pending_alerts()

        assert len(capture.outbox) == 1
        mail = capture.outbox[0]
        assert mail.to == "ops@example.com"
        assert "RuntimeError" in mail.subject
        assert "/boom/{order_id}" in mail.text
        assert "req-abc123" in mail.text
        assert "test_observability.py" in mail.text  # traceback frames
        # Never cookies, never raw secrets from the message.
        assert "SECRETCOOKIE" not in mail.text
        assert "abcdefghijklmnopqrstuvwxyz" not in mail.text

        # Same crash again (different id in the path) -> suppressed.
        resp = await c.post("/boom/456", content=b"password=hunter2")
        assert resp.status_code == 500
        await ops_alert.wait_for_pending_alerts()
        assert len(capture.outbox) == 1


async def test_suppressed_repeats_are_reported_in_the_next_alert() -> None:
    sig = "RuntimeError|x.py:1|/p"
    assert ops_alert._should_send(sig, now=0).send
    assert not ops_alert._should_send(sig, now=10).send
    assert not ops_alert._should_send(sig, now=20).send
    decision = ops_alert._should_send(sig, now=16 * 60)
    assert decision.send
    assert decision.suppressed == 2


async def test_no_alert_without_ops_alert_email(settings) -> None:
    settings.ops_alert_email = ""
    app, capture = _app_with_boom(settings)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.get("/boom/1")
        assert resp.status_code == 500
    await ops_alert.wait_for_pending_alerts()
    assert capture.outbox == []


async def test_readyz_reports_unreachable_s3_and_caches_the_probe(settings, monkeypatch) -> None:
    settings.readyz_check_s3 = True
    calls = []

    def failing_probe():
        calls.append(1)
        raise ConnectionError("no route to host")

    monkeypatch.setattr(health, "_probe_s3_blocking", failing_probe)
    app = create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        first = await c.get("/readyz")
        second = await c.get("/readyz")
        assert first.status_code == 503
        assert first.json()["detail"] == "s3_unreachable: ConnectionError"
        assert second.status_code == 503
        assert len(calls) == 1  # cached

        # /healthz stays static — the deploy gate must not depend on S3.
        assert (await c.get("/healthz")).status_code == 200


async def test_readyz_ok_when_s3_reachable(settings, monkeypatch) -> None:
    settings.readyz_check_s3 = True
    monkeypatch.setattr(health, "_probe_s3_blocking", lambda: None)
    app = create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
