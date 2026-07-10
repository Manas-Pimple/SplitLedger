import enum
from datetime import date
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, Enum, ForeignKey, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDv7PkMixin


class ExpenseCategory(enum.StrEnum):
    rent = "rent"
    utilities = "utilities"
    groceries = "groceries"
    household = "household"
    other = "other"


class ExpenseStatus(enum.StrEnum):
    active = "active"
    reversed = "reversed"


# Single instance shared with recurring_bills so the PG type is created exactly once
expense_category_enum = Enum(ExpenseCategory, name="expense_category")


class Expense(UUIDv7PkMixin, TimestampMixin, Base):
    __tablename__ = "expenses"
    __table_args__ = (CheckConstraint("amount_cents > 0", name="amount_positive"),)

    house_id: Mapped[UUID] = mapped_column(ForeignKey("houses.id"))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    paid_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    description: Mapped[str]
    category: Mapped[ExpenseCategory] = mapped_column(expense_category_enum)
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    split_rule_id: Mapped[UUID] = mapped_column(ForeignKey("split_rules.id"))
    period_start: Mapped[date | None]
    period_end: Mapped[date | None]
    document_id: Mapped[UUID | None] = mapped_column(ForeignKey("documents.id"))
    recurring_bill_id: Mapped[UUID | None] = mapped_column(ForeignKey("recurring_bills.id"))
    status: Mapped[ExpenseStatus] = mapped_column(
        Enum(ExpenseStatus, name="expense_status"), server_default=text("'active'")
    )


class ExpenseShare(UUIDv7PkMixin, TimestampMixin, Base):
    __tablename__ = "expense_shares"

    expense_id: Mapped[UUID] = mapped_column(ForeignKey("expenses.id"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    share_cents: Mapped[int] = mapped_column(BigInteger)
