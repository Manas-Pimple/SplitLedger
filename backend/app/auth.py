from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import ApiError
from app.models import HouseMembership, User
from app.models.auth import RefreshToken
from app.models.house import MembershipStatus
from app.permissions import Principal
from app.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterIn(BaseModel):
    email: EmailStr
    password: str
    display_name: str


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class RefreshIn(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str


class UserOut(BaseModel):
    id: UUID
    email: str
    display_name: str
    is_platform_admin: bool


async def current_principal(
    session: Annotated[AsyncSession, Depends(get_session)],
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    if not authorization or not authorization.startswith("Bearer "):
        raise ApiError(401, "UNAUTHENTICATED", "Missing bearer token")
    user_id = decode_access_token(authorization.removeprefix("Bearer "))
    if user_id is None:
        raise ApiError(401, "UNAUTHENTICATED", "Invalid or expired token")
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise ApiError(401, "UNAUTHENTICATED", "Account unavailable")
    return Principal(user_id=user.id, is_platform_admin=user.is_platform_admin)


async def _issue_tokens(session: AsyncSession, user_id: UUID) -> TokenPair:
    token, token_hash, expires_at = new_refresh_token()
    session.add(RefreshToken(token_hash=token_hash, user_id=user_id, expires_at=expires_at))
    await session.commit()
    return TokenPair(access_token=create_access_token(user_id), refresh_token=token)


@router.post("/register", status_code=201)
async def register(
    body: RegisterIn, session: Annotated[AsyncSession, Depends(get_session)]
) -> UserOut:
    user = User(
        email=body.email,
        password_hash=hash_password(body.password),
        display_name=body.display_name,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        raise ApiError(409, "CONFLICT", "Email already registered") from None
    return UserOut.model_validate(user, from_attributes=True)


@router.post("/login")
async def login(
    body: LoginIn, session: Annotated[AsyncSession, Depends(get_session)]
) -> TokenPair:
    result = await session.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(user.password_hash, body.password):
        raise ApiError(401, "UNAUTHENTICATED", "Invalid credentials")
    return await _issue_tokens(session, user.id)


async def _valid_refresh_row(session: AsyncSession, token: str) -> RefreshToken:
    row = await session.get(RefreshToken, hash_refresh_token(token))
    if row is None or row.revoked_at is not None or row.expires_at < datetime.now(UTC):
        raise ApiError(401, "UNAUTHENTICATED", "Invalid refresh token")
    return row


@router.post("/refresh")
async def refresh(
    body: RefreshIn, session: Annotated[AsyncSession, Depends(get_session)]
) -> TokenPair:
    row = await _valid_refresh_row(session, body.refresh_token)
    row.revoked_at = datetime.now(UTC)  # rotation: single use
    return await _issue_tokens(session, row.user_id)


@router.post("/logout", status_code=204)
async def logout(
    body: RefreshIn, session: Annotated[AsyncSession, Depends(get_session)]
) -> None:
    row = await _valid_refresh_row(session, body.refresh_token)
    row.revoked_at = datetime.now(UTC)
    await session.commit()


me_router = APIRouter(tags=["auth"])


class MembershipOut(BaseModel):
    house_id: UUID
    role: str


class MeOut(UserOut):
    memberships: list[MembershipOut]


@me_router.get("/me")
async def me(
    principal: Annotated[Principal, Depends(current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MeOut:
    user = await session.get(User, principal.user_id)
    assert user is not None
    result = await session.execute(
        select(HouseMembership).where(
            HouseMembership.user_id == user.id,
            HouseMembership.status == MembershipStatus.active,
        )
    )
    memberships = [
        MembershipOut(house_id=m.house_id, role=m.role) for m in result.scalars()
    ]
    return MeOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_platform_admin=user.is_platform_admin,
        memberships=memberships,
    )
