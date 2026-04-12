"""OrderAttachment model — files attached to an order or a single item."""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import Enum, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import AttachmentKind
from app.models.mixins import TenantMixin, TimestampMixin

_ATTACHMENT_KIND = Enum(
    AttachmentKind,
    name="attachment_kind",
    native_enum=False,
    length=16,
    values_callable=lambda obj: [e.value for e in obj],
)


class OrderAttachment(Base, TimestampMixin, TenantMixin):
    __tablename__ = "order_attachments"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)

    order_id: Mapped[UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Optional association with a particular line item.
    order_item_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("order_items.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    kind: Mapped[AttachmentKind] = mapped_column(
        _ATTACHMENT_KIND,
        nullable=False,
        default=AttachmentKind.DOCUMENT,
        server_default=AttachmentKind.DOCUMENT.value,
    )

    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    # The object key inside the S3 bucket. Always prefixed with the tenant's
    # storage_prefix so listing a bucket by key reveals the tenant boundary.
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)

    # Populated after a background task renders a JPEG preview.
    thumbnail_key: Mapped[str | None] = mapped_column(String(512), nullable=True)

    uploaded_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    uploaded_by_contact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("customer_contacts.id", ondelete="SET NULL"), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<OrderAttachment {self.filename} ({self.size_bytes}B)>"
