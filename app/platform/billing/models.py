"""Billing ORM models.

Tables defined here are never tenant-scoped — the ``tenant_id`` FK exists
on ``Subscription`` and ``Invoice`` but they are accessed only by the
platform (owner) role so RLS does not apply. See the module-level
docstring in ``app.platform.billing`` for the demo/live mode split.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class Plan(Base, TimestampMixin):
    __tablename__ = "platform_plans"
    __table_args__ = (UniqueConstraint("code", name="uq_platform_plans_code"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    # Short canonical code: "community", "starter", "pro", "enterprise".
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    stripe_price_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    monthly_price_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CZK")

    # NULL = unlimited.
    max_users: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_contacts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_orders_per_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_storage_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Subscription(Base, TimestampMixin):
    __tablename__ = "platform_subscriptions"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_subs_tenant_id"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    plan_id: Mapped[UUID] = mapped_column(
        ForeignKey("platform_plans.id", ondelete="RESTRICT"),
        nullable=False,
    )

    stripe_customer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # One of: trialing, active, past_due, canceled, incomplete, demo.
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="trialing")

    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Invoice(Base, TimestampMixin):
    __tablename__ = "platform_invoices"
    __table_args__ = (UniqueConstraint("stripe_invoice_id", name="uq_inv_stripe_invoice_id"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    stripe_invoice_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CZK")
    # draft, open, paid, void, uncollectible.
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hosted_invoice_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    pdf_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
