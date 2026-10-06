"""Early access (CEO decision E1): free hosted access until a fixed date.

Every hosted signup is free until ``EARLY_ACCESS_UNTIL`` — the END of that
day in Europe/Prague. Existing trials are never rewritten: the date a
trial really ends is computed on read,

    effective trial end = max(trial_ends_at, early-access end)

for a subscription that is still a local trial (``trialing`` / ``demo``,
not Stripe-managed, not suspended by the operator). Canceled, paid and
operator-suspended subscriptions keep their own dates. Once the date has
passed, today's rules apply again on their own: new signups get the
30-day trial and the max() above stops changing anything for them.

The rule lives in core (not in ``app.platform``) because two core
consumers need it — the periodic jobs in ``app.tasks.periodic`` and the
in-app trial banner in ``app.deps`` — and core may not import the
platform package (CLAUDE.md §6). ``app.platform.billing.early_access``
wraps it for ORM rows; :data:`EFFECTIVE_TRIAL_END_SQL` is the SQL twin
used by the jobs (``tests/test_early_access.py`` checks they agree).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

EARLY_ACCESS_TZ = ZoneInfo("Europe/Prague")

#: Statuses whose end date early access may push out. Everything else
#: (active, past_due, canceled, …) keeps its own dates.
EXTENDABLE_STATUSES: frozenset[str] = frozenset({"trialing", "demo"})

#: Reminder lead times (days before the effective end).
EARLY_ACCESS_BANNER_DAYS = 14


def early_access_ends_at(until: date | None) -> datetime | None:
    """The instant early access ends: 23:59:59 of ``until`` in Prague, as UTC.

    The last second of the day (not next midnight) so the stored value
    renders as the same calendar day whether it is shown in UTC or in
    Prague time.
    """
    if until is None:
        return None
    return datetime.combine(until, time(23, 59, 59), tzinfo=EARLY_ACCESS_TZ).astimezone(UTC)


def is_early_access_active(until: date | None, now: datetime | None = None) -> bool:
    """True while early access has not ended yet (date set and in the future)."""
    end = early_access_ends_at(until)
    return end is not None and (now or datetime.now(UTC)) <= end


def is_extendable(
    status: str | None,
    *,
    trial_ends_at: datetime | None,
    stripe_managed: bool,
    operator_suspended: bool,
) -> bool:
    """May early access push this subscription's end date out?

    Only local trials: a Stripe-managed trial converts (and charges) on
    Stripe's own date, so promising "free until …" there would be false.
    A trial without an end date never ends anyway.
    """
    return (
        status in EXTENDABLE_STATUSES
        and trial_ends_at is not None
        and not stripe_managed
        and not operator_suspended
    )


def effective_trial_end_for(
    status: str | None,
    *,
    trial_ends_at: datetime | None,
    stripe_managed: bool,
    operator_suspended: bool,
    early_access_end: datetime | None,
) -> datetime | None:
    """``max(trial_ends_at, early_access_end)`` for an extendable trial,
    ``trial_ends_at`` unchanged for everything else."""
    if early_access_end is None or not is_extendable(
        status,
        trial_ends_at=trial_ends_at,
        stripe_managed=stripe_managed,
        operator_suspended=operator_suspended,
    ):
        return trial_ends_at
    assert trial_ends_at is not None  # is_extendable checked it
    return max(trial_ends_at, early_access_end)


def covered_by_early_access_for(
    status: str | None,
    *,
    trial_ends_at: datetime | None,
    stripe_managed: bool,
    operator_suspended: bool,
    early_access_end: datetime | None,
) -> bool:
    """Does this subscription's trial end *because of* early access?

    True when early access decides the end date (the stored trial would
    end earlier, or exactly then — new signups store the early-access
    end). A trial running past the early-access end is a normal trial.
    """
    if early_access_end is None or not is_extendable(
        status,
        trial_ends_at=trial_ends_at,
        stripe_managed=stripe_managed,
        operator_suspended=operator_suspended,
    ):
        return False
    assert trial_ends_at is not None
    return trial_ends_at <= early_access_end


# SQL twin of :func:`effective_trial_end_for` over ``platform_subscriptions
# AS s``. Bind ``:early_access_end`` (NULL = feature off — GREATEST ignores
# NULLs, so the stored trial end comes back unchanged).
EFFECTIVE_TRIAL_END_SQL = (
    "(CASE WHEN s.status IN ('trialing', 'demo') "
    "       AND s.trial_ends_at IS NOT NULL "
    "       AND s.stripe_subscription_id IS NULL "
    "       AND s.operator_suspended_at IS NULL "
    "  THEN GREATEST(s.trial_ends_at, CAST(:early_access_end AS timestamptz)) "
    "  ELSE s.trial_ends_at END)"
)

# SQL twin of :func:`covered_by_early_access_for`.
COVERED_BY_EARLY_ACCESS_SQL = (
    "(s.status IN ('trialing', 'demo') "
    " AND s.trial_ends_at IS NOT NULL "
    " AND s.stripe_subscription_id IS NULL "
    " AND s.operator_suspended_at IS NULL "
    " AND CAST(:early_access_end AS timestamptz) IS NOT NULL "
    " AND s.trial_ends_at <= CAST(:early_access_end AS timestamptz))"
)


@dataclass(frozen=True)
class EarlyAccessInfo:
    """What a template needs to talk about early access."""

    active: bool
    until: date | None
    ends_at: datetime | None
    #: ``until`` spelled out for the visitor's locale ("31 January 2027").
    until_label: str


def format_until(until: date, locale: str | None) -> str:
    """Long, locale-aware date: 31. ledna 2027 / 31 January 2027 / 31. Januar 2027."""
    from babel.dates import format_date

    loc = (locale or "cs").split("-")[0].split("_")[0] or "cs"
    try:
        if loc == "en":
            return format_date(until, "d MMMM y", locale="en")
        return format_date(until, "long", locale=loc)
    except Exception:  # unknown locale — never break a page over a date
        return until.strftime("%d.%m.%Y")


def early_access_info(
    until: date | None, locale: str | None = None, now: datetime | None = None
) -> EarlyAccessInfo:
    if until is None:
        return EarlyAccessInfo(active=False, until=None, ends_at=None, until_label="")
    return EarlyAccessInfo(
        active=is_early_access_active(until, now),
        until=until,
        ends_at=early_access_ends_at(until),
        until_label=format_until(until, locale),
    )
