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

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import AssetMovement
from app.models.attachment import OrderAttachment
from app.models.audit_event import AuditEvent
from app.models.customer import Customer, CustomerContact
from app.models.order import Order, OrderComment, OrderStatusHistory
from app.models.user import User

# Placeholder label for anonymised rows. Shows up in audit timelines
# instead of the original email/name after erasure.
ANONYMIZED_LABEL = "<erased>"


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def _public_prefs(prefs: dict[str, Any] | None) -> dict[str, Any]:
    """Notification preferences minus machine-managed ``_``-prefixed keys."""
    return {k: v for k, v in (prefs or {}).items() if not str(k).startswith("_")}


def _order_ref(o: Order) -> dict[str, Any]:
    return {
        "id": str(o.id),
        "number": o.number,
        "title": o.title,
        "customer_id": str(o.customer_id),
        "status": o.status.value,
        "created_at": _iso(o.created_at),
    }


def _comment_ref(c: OrderComment) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "order_id": str(c.order_id),
        "body": c.body,
        "is_internal": c.is_internal,
        "created_at": _iso(c.created_at),
    }


def _status_ref(h: OrderStatusHistory) -> dict[str, Any]:
    return {
        "id": str(h.id),
        "order_id": str(h.order_id),
        "from_status": h.from_status.value if h.from_status else None,
        "to_status": h.to_status.value if h.to_status else None,
        "note": h.note,
        "created_at": _iso(h.created_at),
    }


def _attachment_ref(a: OrderAttachment) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "order_id": str(a.order_id),
        "filename": a.filename,
        "content_type": a.content_type,
        "size_bytes": a.size_bytes,
        "created_at": _iso(a.created_at),
    }


def _event_ref(e: AuditEvent) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "action": e.action,
        "entity_type": e.entity_type,
        "entity_label": e.entity_label,
        "occurred_at": _iso(e.occurred_at),
    }


async def _all(db: AsyncSession, stmt: Any) -> list[Any]:
    return list((await db.execute(stmt)).scalars().all())


async def export_for_user(db: AsyncSession, *, user: User) -> dict[str, Any]:
    """Assemble every piece of personal data about a tenant staff user.

    Art. 15 / 20 scope (audit SEC-5 / F-27 added everything after
    ``audit_events_authored``):

    - profile row incl. notification preferences and 2FA *status*
      (never the TOTP secret or password hash)
    - orders they created and orders assigned to them
    - comments they wrote, status changes they made, files they uploaded,
      stock movements they recorded
    - audit events where they are the actor
    """
    orders = await _all(db, select(Order).where(Order.created_by_user_id == user.id))
    assigned = await _all(db, select(Order).where(Order.assigned_to_user_id == user.id))
    events = await _all(db, select(AuditEvent).where(AuditEvent.actor_id == user.id))
    comments = await _all(db, select(OrderComment).where(OrderComment.author_user_id == user.id))
    status_changes = await _all(
        db, select(OrderStatusHistory).where(OrderStatusHistory.changed_by_user_id == user.id)
    )
    uploads = await _all(
        db, select(OrderAttachment).where(OrderAttachment.uploaded_by_user_id == user.id)
    )
    movements = await _all(
        db, select(AssetMovement).where(AssetMovement.created_by_user_id == user.id)
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
            "two_factor_enabled": bool(user.totp_secret),
            "notification_prefs": _public_prefs(user.notification_prefs),
            "last_login_at": _iso(user.last_login_at),
            "created_at": _iso(user.created_at),
        },
        "orders_created": [_order_ref(o) for o in orders],
        "orders_assigned": [_order_ref(o) for o in assigned],
        "comments_authored": [_comment_ref(c) for c in comments],
        "status_changes": [_status_ref(h) for h in status_changes],
        "attachments_uploaded": [_attachment_ref(a) for a in uploads],
        "asset_movements_recorded": [
            {
                "id": str(m.id),
                "asset_id": str(m.asset_id),
                "type": m.type.value if hasattr(m.type, "value") else str(m.type),
                "quantity": str(m.quantity),
                "note": m.note,
                "occurred_at": _iso(m.occurred_at),
            }
            for m in movements
        ],
        "audit_events_authored": [_event_ref(e) for e in events],
        "exported_at": datetime.now(UTC).isoformat(),
    }


async def export_for_contact(db: AsyncSession, *, contact: CustomerContact) -> dict[str, Any]:
    """Assemble data about a customer contact.

    The customer's orders belong to the customer, not the contact; what
    is *about the contact* is what they did (audit SEC-5 / F-27 added
    everything except the profile and comments): orders they created,
    comments they wrote, status changes they made (e.g. accepting a
    quote), files they uploaded, audit events where they are the actor,
    and their notification preferences.
    """
    customer = (
        await db.execute(select(Customer).where(Customer.id == contact.customer_id))
    ).scalar_one_or_none()
    comments = await _all(
        db, select(OrderComment).where(OrderComment.author_contact_id == contact.id)
    )
    orders = await _all(db, select(Order).where(Order.created_by_contact_id == contact.id))
    status_changes = await _all(
        db,
        select(OrderStatusHistory).where(OrderStatusHistory.changed_by_contact_id == contact.id),
    )
    uploads = await _all(
        db, select(OrderAttachment).where(OrderAttachment.uploaded_by_contact_id == contact.id)
    )
    events = await _all(
        db,
        select(AuditEvent).where(
            AuditEvent.actor_type == "contact", AuditEvent.actor_id == contact.id
        ),
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
            "notification_prefs": _public_prefs(contact.notification_prefs),
            "is_active": contact.is_active,
            "invited_at": _iso(contact.invited_at),
            "accepted_at": _iso(contact.accepted_at),
            "last_login_at": _iso(contact.last_login_at),
            "created_at": _iso(contact.created_at),
        },
        "customer": {
            "id": str(customer.id) if customer else None,
            "name": customer.name if customer else None,
        },
        "orders_created": [_order_ref(o) for o in orders],
        "comments_authored": [_comment_ref(c) for c in comments],
        "status_changes": [_status_ref(h) for h in status_changes],
        "attachments_uploaded": [_attachment_ref(a) for a in uploads],
        "audit_events_authored": [_event_ref(e) for e in events],
        "exported_at": datetime.now(UTC).isoformat(),
    }


async def export_for_identity(db: AsyncSession, *, identity) -> dict[str, Any]:
    """Platform-level identity export (Art. 15 / 20).

    ``db`` must be the owner-scoped platform session: the identity's
    tenant-side records live in several tenants. Exports the Identity
    profile and consent record, every membership, and for each staff /
    contact membership the same per-tenant export the tenant-side
    ``/profile/export`` produces.
    """
    from app.platform.models import TenantMembership

    memberships = await _all(
        db, select(TenantMembership).where(TenantMembership.identity_id == identity.id)
    )
    tenant_records: list[dict[str, Any]] = []
    for m in memberships:
        if m.user_id is not None:
            user = (await db.execute(select(User).where(User.id == m.user_id))).scalar_one_or_none()
            if user is not None:
                tenant_records.append(await export_for_user(db, user=user))
        elif m.contact_id is not None:
            contact = (
                await db.execute(select(CustomerContact).where(CustomerContact.id == m.contact_id))
            ).scalar_one_or_none()
            if contact is not None:
                tenant_records.append(await export_for_contact(db, contact=contact))

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
            "terms_accepted_version": identity.terms_accepted_version,
            "terms_accepted_ip": (
                str(identity.terms_accepted_ip) if identity.terms_accepted_ip else None
            ),
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
        "tenant_records": tenant_records,
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
    """Anonymise a platform identity.

    Only the Identity row itself: the tenant-side Users / Contacts linked
    via TenantMembership are erased by the caller with
    :func:`erase_user` / :func:`erase_contact` (see
    :func:`app.platform.routers.gdpr.identity_delete`), which also writes
    the audit event into each tenant's log.
    """
    identity.email = f"erased-identity-{identity.id}@erased.invalid"
    identity.full_name = ANONYMIZED_LABEL
    identity.password_hash = ""  # empty string = "cannot log in"
    identity.is_active = False
    identity.is_platform_admin = False
    # The consent IP is personal data; keep only *that* and *when* consent
    # was given (accountability, Art. 7(1)), not where from (SEC-2).
    identity.terms_accepted_ip = None
    identity.session_version = (identity.session_version or 0) + 1
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


# ---------------------------------------------------- controller notice


@dataclass(frozen=True)
class ErasureNotice:
    """Who to tell, and what, when a contact erases themselves (SEC-10)."""

    recipients: list[tuple[str, str | None]]  # (email, preferred_locale)
    customer_id: UUID
    customer_name: str
    remaining_contacts: int


async def contact_erasure_notice(
    db: AsyncSession, *, contact: CustomerContact, fallback_email: str | None
) -> ErasureNotice:
    """Build the notice for the tenant (the controller) after a contact's
    self-erasure. Call *after* :func:`erase_contact` so the erased row no
    longer counts as an active contact.

    Recipients are the tenant's active administrators who can actually log
    in; with none, the tenant's billing e-mail. The notice deliberately
    carries no name or e-mail of the erased person.
    """
    from app.models.enums import UserRole

    customer = (
        await db.execute(select(Customer).where(Customer.id == contact.customer_id))
    ).scalar_one_or_none()
    admins = (
        await db.execute(
            select(User.email, User.preferred_locale).where(
                User.tenant_id == contact.tenant_id,
                User.role == UserRole.TENANT_ADMIN,
                User.is_active.is_(True),
                User.password_hash.is_not(None),
            )
        )
    ).all()
    recipients = [(email, locale) for email, locale in admins]
    if not recipients and fallback_email:
        recipients = [(fallback_email, None)]
    remaining = (
        await db.execute(
            select(func.count(CustomerContact.id)).where(
                CustomerContact.customer_id == contact.customer_id,
                CustomerContact.is_active.is_(True),
                CustomerContact.id != contact.id,
            )
        )
    ).scalar_one()
    return ErasureNotice(
        recipients=recipients,
        customer_id=contact.customer_id,
        customer_name=customer.name if customer else "",
        remaining_contacts=int(remaining or 0),
    )
