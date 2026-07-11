import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import ApiError
from app.models import Balance, House, HouseInvite, HouseMembership, HouseSeqCounter, User
from app.models.house import MembershipRole, MembershipStatus
from app.permissions import (
    AuthContext,
    Permission,
    Principal,
    current_principal,
    require,
    resolve_role,
)

router = APIRouter(tags=["houses"])

Session = Annotated[AsyncSession, Depends(get_session)]

INVITE_TTL = timedelta(days=7)
INVITE_MAX_USES = 10


class HouseCreateIn(BaseModel):
    name: str = Field(min_length=1)
    currency: str = Field(default="AUD", min_length=3, max_length=3)


class HousePatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    settings: dict[str, Any] | None = None


class MemberOut(BaseModel):
    user_id: UUID
    display_name: str
    role: MembershipRole
    away_days: list[dict[str, str]]


class HouseOut(BaseModel):
    id: UUID
    name: str
    currency: str
    settings: dict[str, Any]
    members: list[MemberOut] = []


class AwayRange(BaseModel):
    start: str  # ISO dates; stored as-is in jsonb
    end: str


class MemberPatchIn(BaseModel):
    role: MembershipRole | None = None
    away_days: list[AwayRange] | None = None


class InviteOut(BaseModel):
    code: str
    expires_at: datetime


async def _house_out(session: AsyncSession, house: House) -> HouseOut:
    rows = await session.execute(
        select(HouseMembership, User.display_name)
        .join(User, User.id == HouseMembership.user_id)
        .where(
            HouseMembership.house_id == house.id,
            HouseMembership.status == MembershipStatus.active,
        )
    )
    members = [
        MemberOut(
            user_id=m.user_id, display_name=name, role=m.role, away_days=m.away_days
        )
        for m, name in rows
    ]
    return HouseOut(
        id=house.id, name=house.name, currency=house.currency,
        settings=house.settings, members=members,
    )


@router.post("/houses", status_code=201)
async def create_house(
    body: HouseCreateIn,
    principal: Annotated[Principal, Depends(current_principal)],
    session: Session,
) -> HouseOut:
    house = House(name=body.name, currency=body.currency.upper())
    session.add(house)
    await session.flush()
    session.add(
        HouseMembership(
            house_id=house.id, user_id=principal.user_id, role=MembershipRole.manager
        )
    )
    # Seed the per-house event sequence now; Phase 4's ledger writes rely on it
    session.add(HouseSeqCounter(house_id=house.id, next_seq=1))
    await session.commit()
    return await _house_out(session, house)


@router.get("/houses/{house_id}")
async def get_house(
    ctx: Annotated[AuthContext, Depends(require(Permission.VIEW_LEDGER))],
    session: Session,
) -> HouseOut:
    house = await session.get(House, ctx.house_id)
    assert house is not None  # membership implies existence
    return await _house_out(session, house)


@router.patch("/houses/{house_id}")
async def patch_house(
    body: HousePatchIn,
    ctx: Annotated[AuthContext, Depends(require(Permission.HOUSE_SETTINGS_EDIT))],
    session: Session,
) -> HouseOut:
    house = await session.get(House, ctx.house_id)
    assert house is not None
    if body.name is not None:
        house.name = body.name
    if body.settings is not None:
        house.settings = body.settings
    await session.commit()
    return await _house_out(session, house)


@router.post("/houses/{house_id}/invites", status_code=201)
async def create_invite(
    ctx: Annotated[AuthContext, Depends(require(Permission.MEMBER_MANAGE))],
    session: Session,
) -> InviteOut:
    invite = HouseInvite(
        house_id=ctx.house_id,
        code=secrets.token_urlsafe(6),
        expires_at=datetime.now(UTC) + INVITE_TTL,
        max_uses=INVITE_MAX_USES,
    )
    session.add(invite)
    await session.commit()
    return InviteOut(code=invite.code, expires_at=invite.expires_at)


@router.post("/invites/{code}/accept")
async def accept_invite(
    code: str,
    principal: Annotated[Principal, Depends(current_principal)],
    session: Session,
) -> dict[str, UUID]:
    invite = (
        await session.execute(select(HouseInvite).where(HouseInvite.code == code))
    ).scalar_one_or_none()
    if invite is None or invite.expires_at < datetime.now(UTC):
        raise ApiError(404, "NOT_FOUND", "Invite not found or expired")
    if invite.use_count >= invite.max_uses:
        raise ApiError(409, "CONFLICT", "Invite fully used")

    membership = (
        await session.execute(
            select(HouseMembership).where(
                HouseMembership.house_id == invite.house_id,
                HouseMembership.user_id == principal.user_id,
            )
        )
    ).scalar_one_or_none()
    if membership is not None and membership.status == MembershipStatus.active:
        raise ApiError(409, "CONFLICT", "Already a member")
    if membership is not None:  # left earlier (with zero balance) — rejoin as member
        membership.status = MembershipStatus.active
        membership.role = MembershipRole.member
    else:
        session.add(
            HouseMembership(
                house_id=invite.house_id,
                user_id=principal.user_id,
                role=MembershipRole.member,
            )
        )
    invite.use_count += 1
    await session.commit()
    return {"house_id": invite.house_id}


async def _active_manager_count(session: AsyncSession, house_id: UUID) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(HouseMembership)
            .where(
                HouseMembership.house_id == house_id,
                HouseMembership.role == MembershipRole.manager,
                HouseMembership.status == MembershipStatus.active,
            )
        )
    ).scalar_one()


async def _active_membership(
    session: AsyncSession, house_id: UUID, user_id: UUID
) -> HouseMembership:
    m = (
        await session.execute(
            select(HouseMembership).where(
                HouseMembership.house_id == house_id,
                HouseMembership.user_id == user_id,
                HouseMembership.status == MembershipStatus.active,
            )
        )
    ).scalar_one_or_none()
    if m is None:
        raise ApiError(404, "NOT_FOUND", "Member not found")
    return m


@router.patch("/houses/{house_id}/members/{user_id}")
async def patch_member(
    house_id: UUID,
    user_id: UUID,
    body: MemberPatchIn,
    principal: Annotated[Principal, Depends(current_principal)],
    session: Session,
) -> MemberOut:
    caller_role = await resolve_role(session, principal.user_id, house_id)
    if caller_role is None:
        raise ApiError(403, "PERMISSION_DENIED", "Not a member of this house")
    editing_self_away_only = user_id == principal.user_id and body.role is None
    if not editing_self_away_only and caller_role != MembershipRole.manager:
        raise ApiError(403, "PERMISSION_DENIED", "Manager role required")

    membership = await _active_membership(session, house_id, user_id)
    if body.role is not None and body.role != membership.role:
        if (
            membership.role == MembershipRole.manager
            and await _active_manager_count(session, house_id) == 1
        ):
            raise ApiError(409, "CONFLICT", "House must keep at least one manager")
        membership.role = body.role
    if body.away_days is not None:
        membership.away_days = [r.model_dump() for r in body.away_days]
    await session.commit()

    user = await session.get(User, user_id)
    assert user is not None
    return MemberOut(
        user_id=user_id,
        display_name=user.display_name,
        role=membership.role,
        away_days=membership.away_days,
    )


@router.post("/houses/{house_id}/members/{user_id}/leave", status_code=204)
async def leave_house(
    house_id: UUID,
    user_id: UUID,
    principal: Annotated[Principal, Depends(current_principal)],
    session: Session,
) -> None:
    caller_role = await resolve_role(session, principal.user_id, house_id)
    if caller_role is None:
        raise ApiError(403, "PERMISSION_DENIED", "Not a member of this house")
    if user_id != principal.user_id and caller_role != MembershipRole.manager:
        raise ApiError(403, "PERMISSION_DENIED", "Manager role required to remove others")

    membership = await _active_membership(session, house_id, user_id)

    balance = (
        await session.execute(
            select(Balance.balance_cents).where(
                Balance.house_id == house_id, Balance.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if balance not in (None, 0):
        raise ApiError(409, "CONFLICT", "Balance must be zero to leave")
    if (
        membership.role == MembershipRole.manager
        and await _active_manager_count(session, house_id) == 1
    ):
        raise ApiError(409, "CONFLICT", "House must keep at least one manager")

    membership.status = MembershipStatus.left
    await session.commit()
