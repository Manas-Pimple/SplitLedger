import enum
from uuid import UUID

from sqlalchemy import BigInteger, Enum, ForeignKey, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDv7PkMixin


class DocumentStatus(enum.StrEnum):
    pending = "pending"
    uploaded = "uploaded"


class Document(UUIDv7PkMixin, TimestampMixin, Base):
    __tablename__ = "documents"

    house_id: Mapped[UUID] = mapped_column(ForeignKey("houses.id"))
    uploaded_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    r2_key: Mapped[str]
    content_type: Mapped[str]
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, name="document_status"), server_default=text("'pending'")
    )
