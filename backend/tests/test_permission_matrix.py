"""ROLES_AND_PERMISSIONS.md §2 matrix transcribed as the fixture, parametrised
over every (role, permission) pair — the done-gate test for Phase 2."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.house import MembershipRole, MembershipStatus
from app.permissions import POLICY, Permission, resolve_role
from tests.factories import make_house, make_membership, make_user

# (permission, member allowed, manager allowed) — straight from the doc table
MATRIX = [
    (Permission.VIEW_LEDGER, True, True),
    (Permission.EXPENSE_CREATE, True, True),
    (Permission.EXPENSE_CREATE_ON_BEHALF, False, True),
    (Permission.EXPENSE_REVERSE, True, True),  # member: own only (service guard)
    (Permission.DOCUMENT_UPLOAD, True, True),
    (Permission.SETTLEMENT_RECORD, True, True),
    (Permission.DISPUTE_OPEN, True, True),  # share-holder only (service guard)
    (Permission.DISPUTE_COMMENT, True, True),
    (Permission.DISPUTE_RESOLVE, False, True),
    (Permission.SPLIT_RULE_MANAGE, False, True),
    (Permission.RECURRING_BILL_MANAGE, False, True),
    (Permission.MEMBER_MANAGE, False, True),
    (Permission.HOUSE_SETTINGS_EDIT, False, True),
    (Permission.HOUSE_LEAVE, True, True),
]


def test_matrix_covers_every_permission() -> None:
    assert {p for p, _, _ in MATRIX} == set(Permission)


@pytest.mark.parametrize(("permission", "member_ok", "manager_ok"), MATRIX)
def test_policy_matches_matrix(
    permission: Permission, member_ok: bool, manager_ok: bool
) -> None:
    assert (permission in POLICY[MembershipRole.member]) is member_ok
    assert (permission in POLICY[MembershipRole.manager]) is manager_ok


async def test_resolve_role_member_and_manager(session: AsyncSession) -> None:
    house = await make_house(session)
    member = await make_user(session)
    manager = await make_user(session)
    await make_membership(session, house, member, MembershipRole.member)
    await make_membership(session, house, manager, MembershipRole.manager)
    await session.commit()
    assert await resolve_role(session, member.id, house.id) == MembershipRole.member
    assert await resolve_role(session, manager.id, house.id) == MembershipRole.manager


async def test_resolve_role_none_for_outsiders(session: AsyncSession) -> None:
    house = await make_house(session)
    other_house = await make_house(session)
    outsider = await make_user(session)
    left = await make_user(session)
    admin = await make_user(session)
    admin.is_platform_admin = True
    membership = await make_membership(session, house, left, MembershipRole.member)
    membership.status = MembershipStatus.left
    # cross-house isolation: role in one house grants nothing in another
    await make_membership(session, other_house, outsider, MembershipRole.manager)
    await session.commit()

    assert await resolve_role(session, outsider.id, house.id) is None
    assert await resolve_role(session, left.id, house.id) is None
    # platform admin is NOT a superset of house roles — the privacy boundary
    assert await resolve_role(session, admin.id, house.id) is None
