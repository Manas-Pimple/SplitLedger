import enum
from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, Enum, ForeignKey, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDv7PkMixin
from app.models.expense import ExpenseCategory, expense_category_enum


class BillFrequency(enum.StrEnum):
    weekly = "weekly"
    fortnightly = "fortnightly"
    monthly = "monthly"


class RecurringBill(UUIDv7PkMixin, TimestampMixin, Base):
    __tablename__ = "recurring_bills"

    house_id: Mapped[UUID] = mapped_column(ForeignKey("houses.id"))
    description: Mapped[str]
    category: Mapped[ExpenseCategory] = mapped_column(expense_category_enum)
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    split_rule_id: Mapped[UUID] = mapped_column(ForeignKey("split_rules.id"))
    paid_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    frequency: Mapped[BillFrequency] = mapped_column(Enum(BillFrequency, name="bill_frequency"))
    anchor_day: Mapped[int]
    next_run_at: Mapped[datetime]
    is_paused: Mapped[bool] = mapped_column(server_default=text("false"))
