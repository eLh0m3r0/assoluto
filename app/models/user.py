"""User model — tenant staff (supplier employees).

These are the users that log in on behalf of the supplier (e.g. 4MEX).
Distinct from `CustomerContact`, which represents a user on the client side.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, Enum, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import UserRole
from app.models.mixins import TenantMixin, TimestampMixin
from app.models.tenant import JsonColumn


class User(Base, TimestampMixin, TenantMixin):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_users_tenant_id_email"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)

    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)

    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", native_enum=False, length=32),
        nullable=False,
        default=UserRole.TENANT_STAFF,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # Optional TOTP secret (2FA). None = 2FA not enabled.
    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Bumped on password change / force logout; compared to session_version
    # stored in the signed cookie to invalidate old sessions.
    session_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    notification_prefs: Mapped[dict[str, Any]] = mapped_column(
        JsonColumn, nullable=False, default=dict, server_default="{}"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User id={self.id} email={self.email!r} role={self.role.value}>"
