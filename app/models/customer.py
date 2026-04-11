"""Customer and CustomerContact models.

A `Customer` is a client company of the tenant (supplier). A
`CustomerContact` is a person on the customer side who logs into the
portal — they are scoped to a single `customer_id` and can only see
data that belongs to that customer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import CustomerContactRole
from app.models.mixins import TenantMixin, TimestampMixin
from app.models.tenant import JsonColumn


class Customer(Base, TimestampMixin, TenantMixin):
    __tablename__ = "customers"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    ico: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    dic: Mapped[str | None] = mapped_column(String(32), nullable=True)

    billing_address: Mapped[dict[str, Any] | None] = mapped_column(JsonColumn, nullable=True)
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Customer id={self.id} name={self.name!r}>"


class CustomerContact(Base, TimestampMixin, TenantMixin):
    __tablename__ = "customer_contacts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_customer_contacts_tenant_id_email"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)

    customer_id: Mapped[UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)

    role: Mapped[CustomerContactRole] = mapped_column(
        Enum(
            CustomerContactRole,
            name="customer_contact_role",
            native_enum=False,
            length=32,
        ),
        nullable=False,
        default=CustomerContactRole.CUSTOMER_USER,
    )

    # None until the invite is accepted and the user sets a password
    # (magic-link logins may keep this None indefinitely).
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    invited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    session_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    notification_prefs: Mapped[dict[str, Any]] = mapped_column(
        JsonColumn, nullable=False, default=dict, server_default="{}"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CustomerContact id={self.id} email={self.email!r} customer_id={self.customer_id}>"
