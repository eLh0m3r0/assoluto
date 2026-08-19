"""Per-recipient notification preferences.

Stored as a JSONB dict on ``User.notification_prefs`` and
``CustomerContact.notification_prefs`` — columns that existed since
migration ``0002`` but went unread until this module. Shape::

    {"events": {"order_comment": false}, "scope": "involved"}

Missing keys fall back to the role default, so the pre-existing ``{}``
rows need no backfill. The nested ``events`` object also keeps the GDPR
eraser's ``_gdpr_erased_at`` marker (written into the same column by
``app.services.gdpr_service``) from ever colliding with an event name.

### Consent vs. relevance

The two settings are read very differently by
:mod:`app.services.notification_service`, and the distinction is the
thing that keeps the system safe to extend:

* ``events`` is **consent**. A recipient who turned an event off is
  never re-added, for any reason.
* ``scope`` is **relevance** — a narrowing heuristic. If it would leave
  an event with nobody to send to, it yields and the full consenting
  audience is used instead.

Tighten ``scope`` freely; it can never produce "the customer heard
nothing about their own order". Never make ``events`` yield.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.models.enums import CustomerContactRole, UserRole


class NotificationEvent(StrEnum):
    """One notifiable thing that can happen to an order.

    Adding a member here needs three more edits: an entry in
    :data:`STAFF_EVENTS` or :data:`CONTACT_EVENTS`, an email template
    triple named after the value, and a call site that builds it.
    """

    ORDER_SUBMITTED = "order_submitted"
    ORDER_CREATED = "order_created"
    ORDER_STATUS_CHANGED = "order_status_changed"
    ORDER_COMMENT = "order_comment"
    ORDER_ATTACHMENT = "order_attachment"
    ORDER_ASSIGNED = "order_assigned"


class NotificationScope(StrEnum):
    """How wide a net a recipient wants.

    ``ALL``      — staff: every order in the tenant; contact: every order
                   of their company.
    ``INVOLVED`` — staff: orders assigned to them; contact: orders they
                   created, commented on, or uploaded a file to.
    """

    ALL = "all"
    INVOLVED = "involved"


class NotificationSide(StrEnum):
    """Which half of the portal a recipient sits on."""

    STAFF = "staff"
    CONTACT = "contact"


#: Events a tenant staff user can receive. ``ORDER_CREATED`` is absent —
#: staff are the ones creating orders for customers, so it would notify
#: them of their own action.
STAFF_EVENTS: tuple[NotificationEvent, ...] = (
    NotificationEvent.ORDER_SUBMITTED,
    NotificationEvent.ORDER_STATUS_CHANGED,
    NotificationEvent.ORDER_COMMENT,
    NotificationEvent.ORDER_ATTACHMENT,
    NotificationEvent.ORDER_ASSIGNED,
)

#: Events a customer contact can receive. ``ORDER_SUBMITTED`` is absent —
#: submitting is the customer's own action; ``ORDER_ASSIGNED`` is an
#: internal supplier concern the customer must not see.
CONTACT_EVENTS: tuple[NotificationEvent, ...] = (
    NotificationEvent.ORDER_CREATED,
    NotificationEvent.ORDER_STATUS_CHANGED,
    NotificationEvent.ORDER_COMMENT,
    NotificationEvent.ORDER_ATTACHMENT,
)

#: Events that always go to one specific person rather than an audience,
#: so the scope filter is meaningless for them.
_SCOPE_EXEMPT: frozenset[NotificationEvent] = frozenset({NotificationEvent.ORDER_ASSIGNED})


def events_for_side(side: NotificationSide) -> tuple[NotificationEvent, ...]:
    return STAFF_EVENTS if side is NotificationSide.STAFF else CONTACT_EVENTS


def _default_scope(side: NotificationSide, role: Any) -> NotificationScope:
    """Role default for the relevance filter.

    Operators default to ``ALL``: an Operator who hears nothing is the
    exact bug this redesign exists to fix, and a shop small enough not to
    assign orders needs everyone to see everything. ``customer_user``
    defaults to ``INVOLVED`` because that is where the noise complaint
    bites hardest, and ``customer_admin`` (``ALL``) still guarantees the
    company as a whole hears about every order.
    """
    if side is NotificationSide.STAFF:
        return NotificationScope.ALL
    if role == CustomerContactRole.CUSTOMER_ADMIN:
        return NotificationScope.ALL
    return NotificationScope.INVOLVED


@dataclass(frozen=True)
class NotificationPrefs:
    """Resolved preferences for one recipient."""

    side: NotificationSide
    events: frozenset[NotificationEvent]
    scope: NotificationScope

    @classmethod
    def defaults(cls, side: NotificationSide, role: Any = None) -> NotificationPrefs:
        return cls(
            side=side,
            events=frozenset(events_for_side(side)),
            scope=_default_scope(side, role),
        )

    @classmethod
    def from_dict(
        cls,
        raw: dict[str, Any] | None,
        *,
        side: NotificationSide,
        role: Any = None,
    ) -> NotificationPrefs:
        """Parse a stored blob, defaulting every missing key.

        Unknown keys — including the GDPR eraser's marker and any event
        name retired in a later release — are ignored rather than
        raising, so a stale row can never break a send.
        """
        base = cls.defaults(side, role)
        if not raw:
            return base

        allowed = set(events_for_side(side))
        enabled = set(base.events)
        stored_events = raw.get("events")
        if isinstance(stored_events, dict):
            for key, value in stored_events.items():
                try:
                    event = NotificationEvent(key)
                except ValueError:
                    continue
                if event not in allowed:
                    continue
                if value:
                    enabled.add(event)
                else:
                    enabled.discard(event)

        scope = base.scope
        with contextlib.suppress(ValueError):
            scope = NotificationScope(str(raw.get("scope", "")).strip().lower())

        return cls(side=side, events=frozenset(enabled), scope=scope)

    def to_dict(self) -> dict[str, Any]:
        """Serialise explicitly — every event of this side gets a value.

        Writing the full map (rather than only the deltas) means a later
        change to the role defaults never silently rewrites a choice the
        user already made on this page.
        """
        return {
            "events": {e.value: (e in self.events) for e in events_for_side(self.side)},
            "scope": self.scope.value,
        }

    def wants(self, event: NotificationEvent) -> bool:
        """Consent check. Absolute — callers must not override this."""
        return event in self.events

    def scope_covers(self, event: NotificationEvent, *, involved: bool) -> bool:
        """Relevance check. Soft — callers fall back when it empties an audience."""
        if event in _SCOPE_EXEMPT:
            return True
        if self.scope is NotificationScope.ALL:
            return True
        return involved


def prefs_for_user(user: Any) -> NotificationPrefs:
    """Resolve prefs for a tenant staff :class:`~app.models.user.User`."""
    return NotificationPrefs.from_dict(
        user.notification_prefs,
        side=NotificationSide.STAFF,
        role=getattr(user, "role", UserRole.TENANT_STAFF),
    )


def prefs_for_contact(contact: Any) -> NotificationPrefs:
    """Resolve prefs for a :class:`~app.models.customer.CustomerContact`."""
    return NotificationPrefs.from_dict(
        contact.notification_prefs,
        side=NotificationSide.CONTACT,
        role=getattr(contact, "role", CustomerContactRole.CUSTOMER_USER),
    )


def parse_form(
    *,
    side: NotificationSide,
    role: Any,
    selected_events: list[str],
    scope: str,
) -> NotificationPrefs:
    """Build prefs from an HTML form post.

    Checkbox semantics: only *checked* boxes are submitted, so the
    absence of a name means "off". The caller must therefore pass every
    checkbox group it rendered, not just the truthy ones.
    """
    allowed = set(events_for_side(side))
    chosen: set[NotificationEvent] = set()
    for raw in selected_events:
        try:
            event = NotificationEvent(raw)
        except ValueError:
            continue
        if event in allowed:
            chosen.add(event)

    try:
        parsed_scope = NotificationScope(str(scope or "").strip().lower())
    except ValueError:
        parsed_scope = _default_scope(side, role)

    return NotificationPrefs(side=side, events=frozenset(chosen), scope=parsed_scope)
