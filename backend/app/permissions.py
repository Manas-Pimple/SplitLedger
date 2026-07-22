"""RBAC per ROLES_AND_PERMISSIONS.md. POLICY is the §2 matrix transcribed —
the single source for house-scoped allow/deny. Object-level rules (own-only
reversal, share-holder dispute, conflict-of-interest) are service-layer guards
added in later phases. Platform admin is deliberately absent here: admin
routes use require_platform_admin, house routes never check the flag."""

import enum
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import ApiError
from app.models.house import HouseMembership, MembershipRole, MembershipStatus
from app.models.user import User
from app.security import decode_access_token


class Permission(enum.StrEnum):
    VIEW_LEDGER = "view_ledger"
    EXPENSE_CREATE = "expense_create"
    EXPENSE_CREATE_ON_BEHALF = "expense_create_on_behalf"
    EXPENSE_REVERSE = "expense_reverse"  # members: own only (service guard)
    DOCUMENT_UPLOAD = "document_upload"
    SETTLEMENT_RECORD = "settlement_record"
    DISPUTE_OPEN = "dispute_open"  # share-holder only (service guard)
    DISPUTE_COMMENT = "dispute_comment"
    DISPUTE_RESOLVE = "dispute_resolve"  # conflict-of-interest guard in service
    SPLIT_RULE_MANAGE = "split_rule_manage"
    RECURRING_BILL_MANAGE = "recurring_bill_manage"
    MEMBER_MANAGE = "member_manage"
    HOUSE_SETTINGS_EDIT = "house_settings_edit"
    HOUSE_LEAVE = "house_leave"


_MEMBER = frozenset(
    {
        Permission.VIEW_LEDGER,
        Permission.EXPENSE_CREATE,
        Permission.EXPENSE_REVERSE,
        Permission.DOCUMENT_UPLOAD,
        Permission.SETTLEMENT_RECORD,
        Permission.DISPUTE_OPEN,
        Permission.DISPUTE_COMMENT,
        Permission.HOUSE_LEAVE,
    }
)

POLICY: dict[MembershipRole, frozenset[Permission]] = {
    MembershipRole.member: _MEMBER,
    MembershipRole.manager: _MEMBER
    | {
        Permission.EXPENSE_CREATE_ON_BEHALF,
        Permission.DISPUTE_RESOLVE,
        Permission.SPLIT_RULE_MANAGE,
        Permission.RECURRING_BILL_MANAGE,
        Permission.MEMBER_MANAGE,
        Permission.HOUSE_SETTINGS_EDIT,
    },
}


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    is_platform_admin: bool


@dataclass(frozen=True)
class AuthContext:
    principal: Principal
    house_id: UUID
    role: MembershipRole


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
    structlog.contextvars.bind_contextvars(user_id=str(user.id))
    return Principal(user_id=user.id, is_platform_admin=user.is_platform_admin)


async def resolve_role(
    session: AsyncSession, user_id: UUID, house_id: UUID
) -> MembershipRole | None:
    """(user, house) -> role. None for non-members and members who left.
    There is no house role without a house context."""
    result = await session.execute(
        select(HouseMembership.role).where(
            HouseMembership.house_id == house_id,
            HouseMembership.user_id == user_id,
            HouseMembership.status == MembershipStatus.active,
        )
    )
    return result.scalar_one_or_none()


def require(permission: Permission):  # type: ignore[no-untyped-def]
    async def dep(
        house_id: UUID,
        principal: Annotated[Principal, Depends(current_principal)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> AuthContext:
        structlog.contextvars.bind_contextvars(house_id=str(house_id))
        role = await resolve_role(session, principal.user_id, house_id)
        if role is None or permission not in POLICY[role]:
            raise ApiError(403, "PERMISSION_DENIED", f"Role does not allow {permission}")
        return AuthContext(principal, house_id, role)

    return dep


async def require_platform_admin(
    principal: Annotated[Principal, Depends(current_principal)],
) -> Principal:
    if not principal.is_platform_admin:
        raise ApiError(403, "PERMISSION_DENIED", "Platform admin required")
    return principal
