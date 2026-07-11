import enum
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Enum, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDv7PkMixin


class MembershipRole(enum.StrEnum):
    member = "member"
    manager = "manager"


class MembershipStatus(enum.StrEnum):
    active = "active"
    left = "left"


class House(UUIDv7PkMixin, TimestampMixin, Base):
    __tablename__ = "houses"

    name: Mapped[str]
    currency: Mapped[str] = mapped_column(String(3), server_default=text("'AUD'"))
    settings: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))


class HouseMembership(UUIDv7PkMixin, TimestampMixin, Base):
    __tablename__ = "house_memberships"
    __table_args__ = (UniqueConstraint("house_id", "user_id"),)

    house_id: Mapped[UUID] = mapped_column(ForeignKey("houses.id"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    role: Mapped[MembershipRole] = mapped_column(Enum(MembershipRole, name="membership_role"))
    status: Mapped[MembershipStatus] = mapped_column(
        Enum(MembershipStatus, name="membership_status"),
        server_default=text("'active'"),
    )
    away_days: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )


class HouseInvite(UUIDv7PkMixin, TimestampMixin, Base):
    __tablename__ = "house_invites"

    house_id: Mapped[UUID] = mapped_column(ForeignKey("houses.id"))
    code: Mapped[str] = mapped_column(unique=True)
    expires_at: Mapped[datetime]
    max_uses: Mapped[int] = mapped_column(server_default=text("1"))
    use_count: Mapped[int] = mapped_column(server_default=text("0"))
