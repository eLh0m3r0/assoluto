"""Unit tests for ``app.services.locale_service.resolve_email_locale``.

Covers the specificity chain: recipient override → customer default →
tenant default → app default. No DB, no async.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.config import Settings
from app.services.locale_service import resolve_email_locale


def _settings(default: str = "cs", supported: str = "cs,en") -> Settings:
    return Settings(DEFAULT_LOCALE=default, SUPPORTED_LOCALES=supported)


def test_empty_chain_falls_through_to_app_default() -> None:
    s = _settings(default="cs")
    assert resolve_email_locale(settings=s) == "cs"


def test_recipient_override_wins() -> None:
    s = _settings(default="cs")
    recipient = SimpleNamespace(preferred_locale="en")
    customer = SimpleNamespace(preferred_locale=None)
    tenant = SimpleNamespace(settings={})
    assert (
        resolve_email_locale(recipient=recipient, customer=customer, tenant=tenant, settings=s)
        == "en"
    )


def test_customer_default_beats_tenant_and_app() -> None:
    s = _settings(default="cs")
    recipient = SimpleNamespace(preferred_locale=None)
    customer = SimpleNamespace(preferred_locale="en")
    tenant = SimpleNamespace(settings={"default_locale": "cs"})
    assert (
        resolve_email_locale(recipient=recipient, customer=customer, tenant=tenant, settings=s)
        == "en"
    )


def test_tenant_default_from_settings_jsonb() -> None:
    s = _settings(default="cs")
    tenant = SimpleNamespace(settings={"default_locale": "en"})
    assert resolve_email_locale(tenant=tenant, settings=s) == "en"


def test_unsupported_locale_falls_through() -> None:
    """A row storing "de" is ignored if "de" isn't compiled."""
    s = _settings(default="cs", supported="cs,en")
    recipient = SimpleNamespace(preferred_locale="de")
    customer = SimpleNamespace(preferred_locale="fr")
    tenant = SimpleNamespace(settings={"default_locale": "en"})
    # First supported match in chain = "en" from tenant settings.
    assert (
        resolve_email_locale(recipient=recipient, customer=customer, tenant=tenant, settings=s)
        == "en"
    )


def test_locale_is_normalised() -> None:
    """Accept "en-US" from DB and collapse to "en"."""
    s = _settings(default="cs")
    recipient = SimpleNamespace(preferred_locale="en-US")
    assert resolve_email_locale(recipient=recipient, settings=s) == "en"


def test_whitespace_in_stored_value_is_tolerated() -> None:
    s = _settings(default="cs")
    recipient = SimpleNamespace(preferred_locale="  EN  ")
    assert resolve_email_locale(recipient=recipient, settings=s) == "en"


def test_recipient_explicit_none_falls_through_to_customer() -> None:
    s = _settings(default="cs")
    recipient = SimpleNamespace(preferred_locale=None)
    customer = SimpleNamespace(preferred_locale="en")
    assert resolve_email_locale(recipient=recipient, customer=customer, settings=s) == "en"


def test_recipient_without_attr_is_tolerated() -> None:
    """A raw object missing ``preferred_locale`` shouldn't crash."""
    s = _settings(default="cs")
    # SimpleNamespace() has no preferred_locale attribute at all.
    recipient = SimpleNamespace()
    assert resolve_email_locale(recipient=recipient, settings=s) == "cs"
