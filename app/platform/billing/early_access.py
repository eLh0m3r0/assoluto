"""Early access (E1) for platform code: the rule applied to ORM rows.

Thin wrapper over :mod:`app.services.early_access`, which holds the one
rule (core needs it too — periodic jobs and the in-app banner). Every
platform surface that shows or acts on a trial's end date goes through
:func:`effective_trial_end` so "when does this trial end?" has exactly
one answer: billing dashboard, platform admin, signup.
"""

from __future__ import annotations

from datetime import datetime

from app.config import Settings
from app.platform.billing.models import Subscription
from app.services.early_access import (
    covered_by_early_access_for,
    early_access_ends_at,
    effective_trial_end_for,
)


def effective_trial_end(subscription: Subscription | None, settings: Settings) -> datetime | None:
    """When the trial really ends: ``max(trial_ends_at, early-access end)``
    for a local trial (``trialing`` / ``demo``); the stored
    ``trial_ends_at`` for canceled, paid, Stripe-managed and
    operator-suspended subscriptions."""
    if subscription is None:
        return None
    return effective_trial_end_for(
        subscription.status,
        trial_ends_at=subscription.trial_ends_at,
        stripe_managed=subscription.stripe_subscription_id is not None,
        operator_suspended=subscription.operator_suspended_at is not None,
        early_access_end=early_access_ends_at(settings.early_access_until),
    )


def covered_by_early_access(subscription: Subscription | None, settings: Settings) -> bool:
    """Is the end of this subscription's trial set by early access?"""
    if subscription is None:
        return False
    return covered_by_early_access_for(
        subscription.status,
        trial_ends_at=subscription.trial_ends_at,
        stripe_managed=subscription.stripe_subscription_id is not None,
        operator_suspended=subscription.operator_suspended_at is not None,
        early_access_end=early_access_ends_at(settings.early_access_until),
    )
