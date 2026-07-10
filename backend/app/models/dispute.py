import enum
from uuid import UUID

from sqlalchemy import Enum, ForeignKey, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDv7PkMixin


class DisputeStatus(enum.StrEnum):
    open = "open"
    under_review = "under_review"
    resolved = "resolved"
    rejected = "rejected"


class ResolutionKind(enum.StrEnum):
    none = "none"
    adjustment = "adjustment"
    full_reversal = "full_reversal"


class Dispute(UUIDv7PkMixin, TimestampMixin, Base):
    __tablename__ = "disputes"

    house_id: Mapped[UUID] = mapped_column(ForeignKey("houses.id"))
    expense_id: Mapped[UUID] = mapped_column(ForeignKey("expenses.id"))
    opened_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str]
    status: Mapped[DisputeStatus] = mapped_column(
        Enum(DisputeStatus, name="dispute_status"), server_default=text("'open'")
    )
    resolution_kind: Mapped[ResolutionKind] = mapped_column(
        Enum(ResolutionKind, name="resolution_kind"), server_default=text("'none'")
    )
    resolution_event_id: Mapped[UUID | None] = mapped_column(ForeignKey("ledger_events.id"))
    resolved_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))


class DisputeComment(UUIDv7PkMixin, TimestampMixin, Base):
    __tablename__ = "dispute_comments"

    dispute_id: Mapped[UUID] = mapped_column(ForeignKey("disputes.id"))
    author_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str]
