"""Domain enumerations used across models, services, and templates."""

from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    """Role of a tenant staff member (supplier employee)."""

    TENANT_ADMIN = "tenant_admin"
    TENANT_STAFF = "tenant_staff"


class CustomerContactRole(StrEnum):
    """Role of a customer-side user inside the portal."""

    CUSTOMER_ADMIN = "customer_admin"  # can invite other contacts
    CUSTOMER_USER = "customer_user"


class OrderStatus(StrEnum):
    """Lifecycle state of an order.

    State machine lives in `app.services.order_service`; this enum is the
    single source of truth for allowed values.
    """

    DRAFT = "draft"
    SUBMITTED = "submitted"
    QUOTED = "quoted"
    CONFIRMED = "confirmed"
    IN_PRODUCTION = "in_production"
    READY = "ready"
    DELIVERED = "delivered"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class AssetMovementType(StrEnum):
    """Direction/kind of an asset inventory movement."""

    RECEIVE = "receive"  # material delivered by customer
    ISSUE = "issue"  # returned to customer
    CONSUME = "consume"  # used up in production
    ADJUST = "adjust"  # stocktaking correction (+/-)


class AttachmentKind(StrEnum):
    """High-level category of an order attachment (for filtering/icons)."""

    DRAWING = "drawing"
    PHOTO = "photo"
    DOCUMENT = "document"
    OTHER = "other"
