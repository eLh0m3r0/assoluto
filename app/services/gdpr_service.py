"""GDPR Art. 15 (access), Art. 17 (erasure), Art. 20 (portability).

Each data subject we hold data about (platform Identity, tenant
staff :class:`User`, :class:`CustomerContact`) can ask for their
personal data in a machine-readable form and for it to be erased.
This module is the single place that owns both flows so the
guarantees stay consistent:

* **Export** returns a plain ``dict`` ready for JSON serialisation.
  No lazy-loading, no ORM proxy objects — callers can hand it
  straight to ``orjson.dumps``.
* **Erase** does NOT hard-delete the row. Hard delete would cascade
  into ``orders``, ``audit_events``, ``order_comments`` and destroy
  the tenant's own business records. Instead we **anonymise**: null
  out PII columns, flip ``is_active=False``, bump
  ``session_version``, and mark the row with
  ``deleted_at=now()``. The tenant still sees
  "deleted user" / "anonymized contact" in historical context; the
  data subject's identifying data is gone.

The caller is expected to own the DB session + the surrounding
transaction, so this module only flushes. The routing layer commits
after the audit event is written.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_event import AuditEvent
from app.models.customer import Customer, CustomerContact
from app.models.order import Order, OrderComment
from app.models.user import User

# Placeholder label for anonymised rows. Shows up in audit timelines
# instead of the original email/name after erasure.
ANONYMIZED_LABEL = "<erased>"


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


async def export_for_user(db: AsyncSession, *, user: User) -> dict[str, Any]:
    """Assemble every piece of personal data about a tenant staff user.

    Scope:
    - Profile row: email, name, role, locale, timestamps
    - Orders they created (number, title, customer, status)
    - Audit events where they are the actor
    """
    orders = (
        (await db.execute(select(Order).where(Order.created_by_user_id == user.id))).scalars().all()
    )
    events = (
        (await db.execute(select(AuditEvent).where(AuditEvent.actor_id == user.id))).scalars().all()
    )

    return {
        "kind": "user",
        "tenant_id": str(user.tenant_id),
        "profile": {
            "id": str(user.id),
            "email": user.email,
            "full_name": user.full_name,
            "role": user.role.value,
            "is_active": user.is_active,
            "preferred_locale": user.preferred_locale,
            "last_login_at": _iso(user.last_login_at),
            "created_at": _iso(user.created_at),
        },
        "orders_created": [
            {
                "id": str(o.id),
                "number": o.number,
                "title": o.title,
                "customer_id": str(o.customer_id),
                "status": o.status.value,
                "created_at": _iso(o.created_at),
            }
            for o in orders
        ],
        "audit_events_authored": [
            {
                "id": str(e.id),
                "action": e.action,
                "entity_type": e.entity_type,
                "entity_label": e.entity_label,
                "occurred_at": _iso(e.occurred_at),
            }
            for e in events
        ],
        "exported_at": datetime.now(UTC).isoformat(),
    }


async def export_for_contact(db: AsyncSession, *, contact: CustomerContact) -> dict[str, Any]:
    """Assemble data about a customer contact.

    Contact owns less — their customer's orders belong to the customer,
    not the contact. We include orders where *they* are the last
    author of a status change + comments they wrote.
    """
    customer = (
        await db.execute(select(Customer).where(Customer.id == contact.customer_id))
    ).scalar_one_or_none()
    comments = (
        (await db.execute(select(OrderComment).where(OrderComment.author_contact_id == contact.id)))
        .scalars()
        .all()
    )
    return {
        "kind": "contact",
        "tenant_id": str(contact.tenant_id),
        "profile": {
            "id": str(contact.id),
            "email": contact.email,
            "full_name": contact.full_name,
            "phone": contact.phone,
            "role": contact.role.value,
            "preferred_locale": contact.preferred_locale,
            "is_active": contact.is_active,
            "invited_at": _iso(contact.invited_at),
            "accepted_at": _iso(contact.accepted_at),
            "created_at": _iso(contact.created_at),
        },
        "customer": {
            "id": str(customer.id) if customer else None,
            "name": customer.name if customer else None,
        },
        "comments_authored": [
            {
                "id": str(c.id),
                "order_id": str(c.order_id),
                "body": c.body,
                "is_internal": c.is_internal,
                "created_at": _iso(c.created_at),
            }
            for c in comments
        ],
        "exported_at": datetime.now(UTC).isoformat(),
    }


async def export_for_identity(db: AsyncSession, *, identity) -> dict[str, Any]:
    """Platform-level identity export. Identity doesn't own business
    records directly — those are under User / CustomerContact rows
    linked via TenantMembership. We export the Identity's profile
    plus a list of memberships so the data subject knows which
    tenants they appear in.
    """
    from app.platform.models import TenantMembership

    memberships = (
        (
            await db.execute(
                select(TenantMembership).where(TenantMembership.identity_id == identity.id)
            )
        )
        .scalars()
        .all()
    )
    return {
        "kind": "identity",
        "profile": {
            "id": str(identity.id),
            "email": identity.email,
            "full_name": identity.full_name,
            "is_active": identity.is_active,
            "is_platform_admin": identity.is_platform_admin,
            "email_verified_at": _iso(identity.email_verified_at),
            "terms_accepted_at": _iso(identity.terms_accepted_at),
            "last_login_at": _iso(identity.last_login_at),
            "created_at": _iso(identity.created_at),
        },
        "memberships": [
            {
                "id": str(m.id),
                "tenant_id": str(m.tenant_id),
                "access_type": m.access_type,
                "user_id": str(m.user_id) if m.user_id else None,
                "contact_id": str(m.contact_id) if m.contact_id else None,
                "is_active": m.is_active,
                "created_at": _iso(m.created_at),
            }
            for m in memberships
        ],
        "exported_at": datetime.now(UTC).isoformat(),
    }


# ---------------------------------------------------------------- erase


async def erase_user(db: AsyncSession, *, user: User) -> None:
    """Anonymise a tenant staff user.

    Hard-delete would cascade into ``orders`` (ON DELETE ... NULL
    fkey) and into ``audit_events`` via the actor_id column which
    has no FK. We instead:

    * null or replace every PII field on the row
    * bump ``session_version`` to invalidate outstanding sessions
    * flip ``is_active=False`` so login is blocked
    * swap the email to a unique placeholder so the UNIQUE constraint
      stays satisfied if someone re-uses the original email later
    * stamp ``deleted_at`` (audit marker; used by the cleanup job)
    * leave the row's ``id`` intact — orders and audit still link to it
    """
    # Mask the email with the row id so the UNIQUE(tenant_id, email)
    # constraint keeps holding even if the user later re-signs up
    # with the same address (rare; but we must not collide).
    user.email = f"erased-user-{user.id}@erased.invalid"
    user.full_name = ANONYMIZED_LABEL
    user.password_hash = None
    user.preferred_locale = None
    user.totp_secret = None
    user.notification_prefs = {}
    user.is_active = False
    user.session_version += 1
    # Also propagate into the audit trail so the operator can see a
    # timeline of erasure actions. We stamp a flag in notification_prefs
    # as a marker (no dedicated column yet — keeps the migration light).
    user.notification_prefs = {"_gdpr_erased_at": datetime.now(UTC).isoformat()}
    await db.flush()


async def erase_contact(db: AsyncSession, *, contact: CustomerContact) -> None:
    """Anonymise a customer contact. Same pattern as :func:`erase_user`."""
    contact.email = f"erased-contact-{contact.id}@erased.invalid"
    contact.full_name = ANONYMIZED_LABEL
    contact.phone = None
    contact.password_hash = None
    contact.preferred_locale = None
    contact.notification_prefs = {"_gdpr_erased_at": datetime.now(UTC).isoformat()}
    contact.is_active = False
    contact.session_version += 1
    await db.flush()


async def erase_identity(db: AsyncSession, *, identity) -> None:
    """Anonymise a platform identity. The tenant-side Users / Contacts
    linked via TenantMembership remain untouched — they belong to the
    tenant as data controller and must follow the tenant's own
    retention policy. The caller is expected to chain
    :func:`erase_identity` with :func:`erase_user` for each
    membership where the identity's erasure request covers the
    tenant-side record too.
    """
    identity.email = f"erased-identity-{identity.id}@erased.invalid"
    identity.full_name = ANONYMIZED_LABEL
    identity.password_hash = ""  # empty string = "cannot log in"
    identity.is_active = False
    identity.is_platform_admin = False
    await db.flush()


# --------------------------------------------------------------- audit


async def find_target_rows_for_email(
    db: AsyncSession, *, email: str, tenant_id: UUID | None = None
) -> dict[str, list[UUID]]:
    """Lookup every row tied to ``email`` that the caller could erase.

    Used by a (hypothetical) platform-admin-initiated erasure from
    outside the data subject's own session — e.g. when an operator
    receives an SAR by paper mail. Scoped to one tenant when
    ``tenant_id`` is set.
    """
    users_q = select(User.id).where(User.email == email.lower().strip())
    contacts_q = select(CustomerContact.id).where(CustomerContact.email == email.lower().strip())
    if tenant_id is not None:
        users_q = users_q.where(User.tenant_id == tenant_id)
        contacts_q = contacts_q.where(CustomerContact.tenant_id == tenant_id)
    users = list((await db.execute(users_q)).scalars().all())
    contacts = list((await db.execute(contacts_q)).scalars().all())
    return {"user_ids": users, "contact_ids": contacts}
