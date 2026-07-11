from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import HouseInvite, HouseMembership, HouseSeqCounter
from app.models.house import MembershipRole, MembershipStatus
from tests.factories import make_house, make_membership, make_user
from tests.helpers import auth


async def test_create_house_creator_is_manager(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    user = await make_user(session)
    await session.commit()

    r = await client.post("/api/v1/houses", json={"name": "Kombi St"}, headers=auth(user))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["members"][0]["role"] == "manager"
    assert body["currency"] == "AUD"

    counter = await session.get(HouseSeqCounter, body["id"])
    assert counter is not None and counter.next_seq == 1


async def test_get_house_members_and_isolation(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house = await make_house(session)
    manager = await make_user(session)
    outsider_manager = await make_user(session)
    other_house = await make_house(session)
    await make_membership(session, house, manager, MembershipRole.manager)
    # manager elsewhere gets nothing here — cross-house isolation
    await make_membership(session, other_house, outsider_manager, MembershipRole.manager)
    await session.commit()

    r = await client.get(f"/api/v1/houses/{house.id}", headers=auth(manager))
    assert r.status_code == 200
    assert len(r.json()["members"]) == 1

    r = await client.get(f"/api/v1/houses/{house.id}", headers=auth(outsider_manager))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "PERMISSION_DENIED"


async def test_patch_house_manager_only(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house = await make_house(session)
    manager = await make_user(session)
    member = await make_user(session)
    await make_membership(session, house, manager, MembershipRole.manager)
    await make_membership(session, house, member)
    await session.commit()

    r = await client.patch(
        f"/api/v1/houses/{house.id}",
        json={"name": "Renamed", "settings": {"reminder_days": [7, 14, 30]}},
        headers=auth(manager),
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed"

    r = await client.patch(
        f"/api/v1/houses/{house.id}", json={"name": "Nope"}, headers=auth(member)
    )
    assert r.status_code == 403


async def test_invite_flow(client: httpx.AsyncClient, session: AsyncSession) -> None:
    house = await make_house(session)
    manager = await make_user(session)
    joiner = await make_user(session)
    await make_membership(session, house, manager, MembershipRole.manager)
    await session.commit()

    # member cannot create invites
    r = await client.post(f"/api/v1/houses/{house.id}/invites", headers=auth(joiner))
    assert r.status_code == 403

    r = await client.post(f"/api/v1/houses/{house.id}/invites", headers=auth(manager))
    assert r.status_code == 201
    code = r.json()["code"]

    r = await client.post(f"/api/v1/invites/{code}/accept", headers=auth(joiner))
    assert r.status_code == 200
    assert r.json()["house_id"] == str(house.id)

    # double-accept → already member
    r = await client.post(f"/api/v1/invites/{code}/accept", headers=auth(joiner))
    assert r.status_code == 409

    # unknown code → 404
    r = await client.post("/api/v1/invites/nope/accept", headers=auth(joiner))
    assert r.status_code == 404


async def test_expired_and_exhausted_invites(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house = await make_house(session)
    user = await make_user(session)
    await session.commit()

    expired = HouseInvite(
        house_id=house.id, code="expired1",
        expires_at=datetime.now(UTC) - timedelta(days=1), max_uses=5,
    )
    exhausted = HouseInvite(
        house_id=house.id, code="usedup1",
        expires_at=datetime.now(UTC) + timedelta(days=1), max_uses=1, use_count=1,
    )
    session.add_all([expired, exhausted])
    await session.commit()

    r = await client.post("/api/v1/invites/expired1/accept", headers=auth(user))
    assert r.status_code == 404
    r = await client.post("/api/v1/invites/usedup1/accept", headers=auth(user))
    assert r.status_code == 409


async def test_leave_and_rejoin(client: httpx.AsyncClient, session: AsyncSession) -> None:
    house = await make_house(session)
    manager = await make_user(session)
    member = await make_user(session)
    await make_membership(session, house, manager, MembershipRole.manager)
    await make_membership(session, house, member)
    await session.commit()

    # capture ids before expire_all detaches ORM attrs
    house_id, member_id, manager_id = house.id, member.id, manager.id

    r = await client.post(
        f"/api/v1/houses/{house_id}/members/{member_id}/leave", headers=auth(member_id)
    )
    assert r.status_code == 204

    async def member_status() -> MembershipStatus:
        session.expire_all()
        return (
            await session.execute(
                select(HouseMembership.status).where(
                    HouseMembership.house_id == house_id,
                    HouseMembership.user_id == member_id,
                )
            )
        ).scalar_one()

    assert await member_status() == MembershipStatus.left

    # rejoin via invite reactivates the same row
    code = (
        await client.post(f"/api/v1/houses/{house_id}/invites", headers=auth(manager_id))
    ).json()["code"]
    r = await client.post(f"/api/v1/invites/{code}/accept", headers=auth(member_id))
    assert r.status_code == 200
    assert await member_status() == MembershipStatus.active


async def test_leave_blocked_by_nonzero_balance(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house = await make_house(session)
    manager = await make_user(session)
    member = await make_user(session)
    await make_membership(session, house, manager, MembershipRole.manager)
    await make_membership(session, house, member)
    await session.execute(
        text(
            "INSERT INTO balances (house_id, user_id, balance_cents) VALUES (:h, :u, -500)"
        ),
        {"h": house.id, "u": member.id},
    )
    await session.commit()

    r = await client.post(
        f"/api/v1/houses/{house.id}/members/{member.id}/leave", headers=auth(member)
    )
    assert r.status_code == 409
    assert "zero" in r.json()["error"]["message"].lower()


async def test_last_manager_protection(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house = await make_house(session)
    manager = await make_user(session)
    member = await make_user(session)
    await make_membership(session, house, manager, MembershipRole.manager)
    await make_membership(session, house, member)
    await session.commit()

    # demote last manager → 409
    r = await client.patch(
        f"/api/v1/houses/{house.id}/members/{manager.id}",
        json={"role": "member"},
        headers=auth(manager),
    )
    assert r.status_code == 409

    # last manager leaving → 409
    r = await client.post(
        f"/api/v1/houses/{house.id}/members/{manager.id}/leave", headers=auth(manager)
    )
    assert r.status_code == 409

    # promote member, then demotion of original manager works
    r = await client.patch(
        f"/api/v1/houses/{house.id}/members/{member.id}",
        json={"role": "manager"},
        headers=auth(manager),
    )
    assert r.status_code == 200
    r = await client.patch(
        f"/api/v1/houses/{house.id}/members/{manager.id}",
        json={"role": "member"},
        headers=auth(manager),
    )
    assert r.status_code == 200


async def test_away_days_permissions(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house = await make_house(session)
    manager = await make_user(session)
    alice = await make_user(session)
    bob = await make_user(session)
    await make_membership(session, house, manager, MembershipRole.manager)
    await make_membership(session, house, alice)
    await make_membership(session, house, bob)
    await session.commit()

    away = {"away_days": [{"start": "2026-08-01", "end": "2026-08-10"}]}

    # member edits own away days — allowed
    r = await client.patch(
        f"/api/v1/houses/{house.id}/members/{alice.id}", json=away, headers=auth(alice)
    )
    assert r.status_code == 200
    assert r.json()["away_days"][0]["start"] == "2026-08-01"

    # member edits someone else's — denied
    r = await client.patch(
        f"/api/v1/houses/{house.id}/members/{bob.id}", json=away, headers=auth(alice)
    )
    assert r.status_code == 403

    # member changes own role — denied (role change needs manager)
    r = await client.patch(
        f"/api/v1/houses/{house.id}/members/{alice.id}",
        json={"role": "manager"},
        headers=auth(alice),
    )
    assert r.status_code == 403

    # manager edits anyone's away days — allowed
    r = await client.patch(
        f"/api/v1/houses/{house.id}/members/{bob.id}", json=away, headers=auth(manager)
    )
    assert r.status_code == 200
