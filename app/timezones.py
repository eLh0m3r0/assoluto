"""Tenant time zones — the one place UTC becomes local time.

Everything is **stored** in UTC (``TIMESTAMPTZ``; asyncpg hands it back
as an aware UTC ``datetime``). Everything a person **reads** — pages,
PDFs, CSV, accounting exports — and every calendar-day question
("what is today?", "orders created on 3 March") is answered in the
tenant's time zone.

Where the zone comes from:

* ``tenants.settings["timezone"]`` — an IANA name picked on
  ``/app/admin/tenant-settings``. No column, no migration.
* ``DEFAULT_TIMEZONE`` (``Settings.default_timezone``) when the tenant
  has none, the stored name is not a valid zone, or there is no tenant
  at all (platform admin, billing).
* ``Europe/Prague`` when even the setting is broken; UTC only if the
  host has no tz database at all.

Plain ``date`` values (requested / promised / delivered dates) are
calendar days, not instants — they are never converted.

The resolved zone of the current request lives on
``request.state.tz`` (set by :func:`app.deps.get_current_tenant`);
templates read it through the ``localtime`` / ``localdate`` filters
(``app.templating``), Python code through :func:`request_tz`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, tzinfo
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

#: Last-resort zone when ``DEFAULT_TIMEZONE`` itself is not valid.
FALLBACK_TIMEZONE = "Europe/Prague"

#: Key inside ``tenants.settings`` holding the tenant's IANA zone name.
SETTINGS_KEY = "timezone"

#: Offered first in the tenant-settings picker — the markets Assoluto
#: sells into, then the two zones people ask for by name.
CURATED_TIMEZONES: tuple[str, ...] = (
    "Europe/Prague",
    "Europe/Berlin",
    "Europe/Vienna",
    "Europe/Bratislava",
    "Europe/Warsaw",
    "Europe/Budapest",
    "Europe/London",
    "UTC",
)

# Region prefixes of "real" zones. Leaves out the legacy aliases
# (``CET``, ``EST5EDT``, ``Etc/GMT+3``, ``US/Pacific`` …) that only add
# noise to a select box; they still validate if stored by hand.
_COMMON_PREFIXES: tuple[str, ...] = (
    "Africa/",
    "America/",
    "Antarctica/",
    "Asia/",
    "Atlantic/",
    "Australia/",
    "Europe/",
    "Indian/",
    "Pacific/",
)


@lru_cache(maxsize=512)
def _zone(name: str) -> ZoneInfo | None:
    """``ZoneInfo(name)`` or ``None`` for anything that is not a zone."""
    if not name or len(name) > 64:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        # ValueError: malformed keys ("../etc/passwd", absolute paths).
        return None


def normalize_timezone(value: Any) -> str | None:
    """Return the IANA name if ``value`` names a real zone, else ``None``."""
    if not isinstance(value, str):
        return None
    name = value.strip()
    return name if _zone(name) is not None else None


def is_valid_timezone(value: Any) -> bool:
    return normalize_timezone(value) is not None


def default_timezone_name() -> str:
    """``DEFAULT_TIMEZONE`` if valid, otherwise :data:`FALLBACK_TIMEZONE`."""
    from app.config import get_settings

    return normalize_timezone(get_settings().default_timezone) or FALLBACK_TIMEZONE


def zone_for(name: str | None) -> tzinfo:
    """Resolve ``name`` to a tzinfo, falling back to the default zone."""
    zone = _zone(name or "") if name else None
    if zone is None:
        zone = _zone(default_timezone_name())
    return zone if zone is not None else UTC


def default_tz() -> tzinfo:
    """The zone for pages without a tenant (platform admin, billing)."""
    return zone_for(default_timezone_name())


def tenant_timezone_name(tenant: Any) -> str:
    """The tenant's configured zone name, or the default when unset/invalid."""
    blob = getattr(tenant, "settings", None) if tenant is not None else None
    return settings_timezone_name(blob)


def settings_timezone_name(blob: Any) -> str:
    """Zone name from a raw ``tenants.settings`` blob (default if absent)."""
    if isinstance(blob, dict):
        name = normalize_timezone(blob.get(SETTINGS_KEY))
        if name:
            return name
    return default_timezone_name()


def tenant_tz(tenant: Any) -> tzinfo:
    """tzinfo for ``tenant`` (``None`` → the default zone)."""
    return zone_for(tenant_timezone_name(tenant))


async def tenant_tz_by_id(db: Any, tenant_id: Any) -> tzinfo:
    """Load just ``tenants.settings`` and resolve the zone.

    For service code that holds a tenant id but not the row (order
    transitions, numbering). ``tenants`` has no RLS, so this works on
    the request session as well as the owner engine.
    """
    from sqlalchemy import select

    from app.models.tenant import Tenant

    blob = (
        await db.execute(select(Tenant.settings).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    return zone_for(settings_timezone_name(blob))


def request_tz(request: Any) -> tzinfo:
    """Zone for the current request: the tenant's, else the default."""
    state = getattr(request, "state", None)
    tz = getattr(state, "tz", None) if state is not None else None
    if isinstance(tz, tzinfo):
        return tz
    tenant = getattr(state, "tenant", None) if state is not None else None
    return tenant_tz(tenant)


def tz_label(tz: tzinfo | None) -> str:
    """IANA name of ``tz`` (``Europe/Prague``) for headers and labels."""
    if tz is None:
        return default_timezone_name()
    return str(getattr(tz, "key", None) or tz)


# ------------------------------------------------------------- conversion


def to_local(value: Any, tz: tzinfo | None) -> Any:
    """Convert an instant to ``tz``. Naive datetimes are taken as UTC.

    Anything that is not a ``datetime`` (``None``, a plain ``date``, a
    string) is returned unchanged — calendar dates are not instants.
    """
    if not isinstance(value, datetime):
        return value
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(tz or default_tz())


def local_date(value: datetime, tz: tzinfo | None) -> date:
    """The calendar day ``value`` falls on in ``tz``."""
    return to_local(value, tz).date()


def local_today(tz: tzinfo | None, *, now: datetime | None = None) -> date:
    """Today's date in ``tz`` (``now`` is injectable for tests)."""
    return local_date(now or datetime.now(UTC), tz)


def local_day_start(day: date, tz: tzinfo | None) -> datetime:
    """The UTC instant at which local calendar day ``day`` begins.

    Used to turn a user's ``from`` / ``to`` dates into ``TIMESTAMPTZ``
    bounds: ``created_at >= start(from)`` and
    ``created_at < start(to + 1 day)``. Correct on DST days (23 h / 25 h
    long) because the start of each day is resolved on its own.
    """
    local_midnight = datetime.combine(day, time.min).replace(tzinfo=tz or default_tz())
    return local_midnight.astimezone(UTC)


def format_local(value: Any, tz: tzinfo | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Render ``value`` in ``tz`` with ``strftime`` ``fmt`` (``""`` for None).

    ``%Z`` in ``fmt`` prints the zone abbreviation (``CET`` / ``CEST``).
    Plain dates are formatted as-is, without conversion.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return to_local(value, tz).strftime(fmt)
    if isinstance(value, date):
        return value.strftime(fmt)
    return str(value)


@lru_cache(maxsize=1)
def timezone_choices() -> tuple[str, ...]:
    """Zones for the picker: curated first, then the rest alphabetically."""
    curated = tuple(z for z in CURATED_TIMEZONES if _zone(z) is not None)
    try:
        everything = available_timezones()
    except Exception:  # pragma: no cover - no tz database on the host
        everything = set()
    rest = sorted(z for z in everything if z.startswith(_COMMON_PREFIXES) and z not in set(curated))
    return curated + tuple(rest)
