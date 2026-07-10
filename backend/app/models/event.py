from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDv7PkMixin


class Event(UUIDv7PkMixin, TimestampMixin, Base):
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("house_id", "seq"),)

    house_id: Mapped[UUID] = mapped_column(ForeignKey("houses.id"))
    seq: Mapped[int] = mapped_column(BigInteger)
    type: Mapped[str]
    payload: Mapped[dict[str, Any]]
    published_at: Mapped[datetime | None]


class HouseSeqCounter(TimestampMixin, Base):
    __tablename__ = "house_seq_counters"

    house_id: Mapped[UUID] = mapped_column(ForeignKey("houses.id"), primary_key=True)
    next_seq: Mapped[int] = mapped_column(BigInteger)


class IdempotencyKey(TimestampMixin, Base):
    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    request_hash: Mapped[str]
    response_status: Mapped[int | None]
    response_body: Mapped[dict[str, Any] | None]
    expires_at: Mapped[datetime]
