from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class RefreshToken(TimestampMixin, Base):
    """Not in DATA_MODEL.md — added per user decision (2026-07-11): rotation and
    logout revocation require server-side state. Record in DECISIONS.md (Phase 14)."""

    __tablename__ = "refresh_tokens"

    token_hash: Mapped[str] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
