import enum
from typing import Any
from uuid import UUID

from sqlalchemy import Enum, ForeignKey, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDv7PkMixin


class SplitRuleKind(enum.StrEnum):
    equal = "equal"
    percentage = "percentage"
    weighted = "weighted"
    equal_prorated = "equal_prorated"


class SplitRule(UUIDv7PkMixin, TimestampMixin, Base):
    __tablename__ = "split_rules"

    house_id: Mapped[UUID] = mapped_column(ForeignKey("houses.id"))
    name: Mapped[str]
    kind: Mapped[SplitRuleKind] = mapped_column(Enum(SplitRuleKind, name="split_rule_kind"))
    config: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    is_archived: Mapped[bool] = mapped_column(server_default=text("false"))
