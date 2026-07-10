from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDv7PkMixin


class Notification(UUIDv7PkMixin, TimestampMixin, Base):
    __tablename__ = "notifications"

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    house_id: Mapped[UUID] = mapped_column(ForeignKey("houses.id"))
    type: Mapped[str]
    payload: Mapped[dict[str, Any]]
    read_at: Mapped[datetime | None]
