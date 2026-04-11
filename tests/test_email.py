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


def test_send_invitation_swallows_exceptions() -> None:
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
