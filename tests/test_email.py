"""Tests for email rendering and the background invitation task."""

from __future__ import annotations

from app.email.sender import CaptureSender, render_email
from app.tasks.email_tasks import send_invitation


def test_render_invitation_email_contains_expected_markers() -> None:
    rendered = render_email(
        "invitation",
        {
            "tenant_name": "4MEX",
            "customer_name": "ACME s.r.o.",
            "contact_name": "Jan Novák",
            "invite_url": "http://example.com/invite/xyz",
        },
    )
    assert "4MEX" in rendered.subject
    assert "Jan Novák" in rendered.html
    assert "ACME s.r.o." in rendered.html
    assert "http://example.com/invite/xyz" in rendered.html
    # Text fallback should also contain the URL and the customer name.
    assert "http://example.com/invite/xyz" in rendered.text
    assert "ACME s.r.o." in rendered.text


def test_send_invitation_uses_capture_sender() -> None:
    sender = CaptureSender()
    send_invitation(
        sender,
        to="jan@acme.cz",
        tenant_name="4MEX",
        customer_name="ACME",
        contact_name="Jan",
        invite_url="http://example.com/invite/abc",
    )
    assert len(sender.outbox) == 1
    msg = sender.outbox[0]
    assert msg.to == "jan@acme.cz"
    assert "4MEX" in msg.subject
    assert "http://example.com/invite/abc" in msg.html


def test_send_invitation_swallows_exceptions(monkeypatch) -> None:
    # Disable the retry backoff so the test is fast; we care about the
    # exception-swallowing invariant, not the retry count.
    import app.tasks.email_tasks as et

    monkeypatch.setattr(et, "_MAX_ATTEMPTS", 1)

    class ExplodingSender:
        def send(self, **kwargs):
            raise RuntimeError("smtp down")

    # Should not raise — the task logs and swallows.
    send_invitation(
        ExplodingSender(),  # type: ignore[arg-type]
        to="x@example.com",
        tenant_name="t",
        customer_name="c",
        contact_name="n",
        invite_url="http://x",
    )


def test_send_invitation_retries_on_transient_failure(monkeypatch) -> None:
    """SMTP blip → first send fails, second succeeds, no error log."""
    import app.tasks.email_tasks as et

    # Collapse the backoff so this test doesn't sleep seconds.
    monkeypatch.setattr(et, "_BACKOFF_BASE_SECONDS", 0)

    calls = {"n": 0}

    class BlipSender:
        def send(self, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("connection reset — transient")

    send_invitation(
        BlipSender(),  # type: ignore[arg-type]
        to="x@example.com",
        tenant_name="t",
        customer_name="c",
        contact_name="n",
        invite_url="http://x",
    )
    assert calls["n"] == 2, "expected one retry after transient failure"


def test_render_invitation_in_english_locale_produces_english_subject() -> None:
    from app.email.sender import _reset_env_cache_for_tests
    from app.i18n import reset_translations_cache

    reset_translations_cache()
    _reset_env_cache_for_tests()

    en = render_email(
        "invitation",
        {
            "tenant_name": "4MEX",
            "customer_name": "ACME s.r.o.",
            "contact_name": "Jan Novák",
            "invite_url": "http://example.com/invite/xyz",
        },
        locale="en",
    )
    assert "Invitation" in en.subject
    assert "4MEX" in en.subject
    # English body uses the imperative "Accept the invitation".
    assert "Accept the invitation" in en.html
    assert "Přijmout" not in en.html


def test_render_invitation_in_czech_locale_produces_czech_body() -> None:
    from app.email.sender import _reset_env_cache_for_tests
    from app.i18n import reset_translations_cache

    reset_translations_cache()
    _reset_env_cache_for_tests()

    cs = render_email(
        "invitation",
        {
            "tenant_name": "4MEX",
            "customer_name": "ACME s.r.o.",
            "contact_name": "Jan Novák",
            "invite_url": "http://example.com/invite/xyz",
        },
        locale="cs",
    )
    assert "Pozvánka" in cs.subject
    assert "4MEX" in cs.subject
    assert "Přijmout pozvánku" in cs.html
    assert "Accept invitation" not in cs.html


def test_render_order_submitted_with_status_label_translates_label() -> None:
    """``status_label`` is passed as the English msgid and translated
    inside the template — verify both locales render it correctly."""
    from app.email.sender import _reset_env_cache_for_tests
    from app.i18n import reset_translations_cache

    reset_translations_cache()
    _reset_env_cache_for_tests()

    ctx = {
        "tenant_name": "4MEX",
        "order_number": "2026-000042",
        "order_title": "Widgets",
        "order_url": "http://x",
        "status_label": "Quoted",
    }
    en = render_email("order_status_changed", ctx, locale="en")
    cs = render_email("order_status_changed", ctx, locale="cs")
    assert "Quoted" in en.subject
    assert "Naceněno" in cs.subject


# ---------------------------------------------------------------------------
# _safe_error_summary redaction
# ---------------------------------------------------------------------------


def test_safe_error_summary_redacts_url_and_query_token() -> None:
    """The pre-existing two patterns (URL + ``=value``) still fire — keeps
    the most common SMTP-relay error shape from leaking embedded reset
    URLs and ``email=…&token=…`` query strings."""
    from app.tasks.email_tasks import _safe_error_summary

    s = _safe_error_summary(
        Exception("Couldn't deliver to https://example.com/reset?token=abcdefghijkl1234")
    )
    assert "https://" not in s
    assert "abcdefghijkl1234" not in s
    assert "[url]" in s


def test_safe_error_summary_redacts_jwt_shape() -> None:
    """JWT-shape three-segment tokens (header.payload.signature, each
    ≥20 base64-url-safe chars) get collapsed to ``[jwt]``."""
    from app.tasks.email_tasks import _safe_error_summary

    jwt_like = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiJ4eXoiLCJpYXQiOjE3MTYwMDAwMDB9"
        ".abcDEFghiJKLmnoPQRstuVWXyz1234567890ABCDEF"
    )
    s = _safe_error_summary(Exception(f"smtp rejected: token {jwt_like} please check"))
    assert "[jwt]" in s
    assert "eyJhbGc" not in s


def test_safe_error_summary_redacts_bearer_header_form() -> None:
    """``Authorization: Bearer <token>`` and ``X-Token: <token>`` style
    header echoes get the token redacted even though there's no leading ``=``."""
    from app.tasks.email_tasks import _safe_error_summary

    s1 = _safe_error_summary(
        Exception("auth failed: Authorization: Bearer abcdef1234567890zzzzzzzzzz")
    )
    assert "abcdef1234567890zzzzzzzzzz" not in s1
    assert "[redacted]" in s1.lower()

    s2 = _safe_error_summary(Exception("X-Token: abcdefghijklmnopqrst123456"))
    assert "abcdefghijklmnopqrst123456" not in s2


def test_safe_error_summary_redacts_long_hex_blob() -> None:
    """Standalone 32+-char hex blobs (signed-token hex variant, raw
    secrets) get collapsed to ``[hex]``."""
    from app.tasks.email_tasks import _safe_error_summary

    s = _safe_error_summary(
        Exception("server returned: 0123456789abcdef0123456789abcdef0123456789abcdef")
    )
    assert "0123456789abcdef" not in s
    assert "[hex]" in s


def test_safe_error_summary_truncates_to_160_chars() -> None:
    """Long messages get truncated to keep structured-log lines bounded."""
    from app.tasks.email_tasks import _safe_error_summary

    s = _safe_error_summary(Exception("x" * 500))
    assert len(s) == 160


def test_safe_error_summary_keeps_human_readable_errors_intact() -> None:
    """Non-secret error text passes through untouched (modulo truncation)."""
    from app.tasks.email_tasks import _safe_error_summary

    s = _safe_error_summary(Exception("554 5.7.1 Recipient address rejected"))
    assert s == "554 5.7.1 Recipient address rejected"
