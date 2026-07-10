import enum
from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, Enum, ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDv7PkMixin


class LedgerEventKind(enum.StrEnum):
    expense = "expense"
    expense_reversal = "expense_reversal"
    settlement = "settlement"
    adjustment = "adjustment"


class SettlementStatus(enum.StrEnum):
    pending = "pending"
    confirmed = "confirmed"
    rejected = "rejected"


class LedgerEvent(UUIDv7PkMixin, TimestampMixin, Base):
    __tablename__ = "ledger_events"

    house_id: Mapped[UUID] = mapped_column(ForeignKey("houses.id"))
    kind: Mapped[LedgerEventKind] = mapped_column(Enum(LedgerEventKind, name="ledger_event_kind"))
    ref_id: Mapped[UUID]
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))


class LedgerEntry(UUIDv7PkMixin, TimestampMixin, Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (Index("ix_ledger_entries_house_user", "house_id", "user_id"),)

    ledger_event_id: Mapped[UUID] = mapped_column(ForeignKey("ledger_events.id"))
    house_id: Mapped[UUID] = mapped_column(ForeignKey("houses.id"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    amount_cents: Mapped[int] = mapped_column(BigInteger)


class Balance(TimestampMixin, Base):
    __tablename__ = "balances"

    house_id: Mapped[UUID] = mapped_column(ForeignKey("houses.id"), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    balance_cents: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    oldest_debt_at: Mapped[datetime | None]


class Settlement(UUIDv7PkMixin, TimestampMixin, Base):
    __tablename__ = "settlements"

    house_id: Mapped[UUID] = mapped_column(ForeignKey("houses.id"))
    from_user: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    to_user: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[SettlementStatus] = mapped_column(
        Enum(SettlementStatus, name="settlement_status"), server_default=text("'pending'")
    )
    method: Mapped[str | None]
