import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import House, User
from app.models.house import MembershipRole
from tests.factories import make_house, make_membership, make_user
from tests.helpers import auth


async def _setup(session: AsyncSession) -> tuple[House, User, User]:
    house = await make_house(session)
    alice = await make_user(session)
    bob = await make_user(session)
    await make_membership(session, house, alice, MembershipRole.manager)
    await make_membership(session, house, bob)
    await session.commit()
    # bob owes alice 3000
    await session.execute(
        text(
            "INSERT INTO balances (house_id, user_id, balance_cents) VALUES "
            "(:h, :a, 3000), (:h, :b, -3000)"
        ),
        {"h": house.id, "a": alice.id, "b": bob.id},
    )
    await session.commit()
    return house, alice, bob


async def _balances(client: httpx.AsyncClient, house_id: str, user: User) -> dict[str, int]:
    r = await client.get(f"/api/v1/houses/{house_id}/balances", headers=auth(user))
    return {b["user_id"]: b["balance_cents"] for b in r.json()}


async def test_suggest_is_advisory_only(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house, alice, bob = await _setup(session)
    r = await client.get(
        f"/api/v1/houses/{house.id}/settlements/suggest", headers=auth(alice)
    )
    assert r.status_code == 200
    transfers = r.json()
    assert transfers == [
        {"from_user": str(bob.id), "to_user": str(alice.id), "amount_cents": 3000}
    ]
    # zero side effects: balances unchanged, no settlement rows created
    balances = await _balances(client, str(house.id), alice)
    assert balances[str(bob.id)] == -3000
    r = await client.get(f"/api/v1/houses/{house.id}/settlements", headers=auth(alice))
    assert r.json()["items"] == []


async def test_record_confirm_moves_balances(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house, alice, bob = await _setup(session)

    r = await client.post(
        f"/api/v1/houses/{house.id}/settlements",
        json={"to_user": str(alice.id), "amount_cents": 3000, "method": "bank transfer"},
        headers=auth(bob),
    )
    assert r.status_code == 201
    settlement = r.json()
    assert settlement["status"] == "pending"

    # done-gate: pending settlement does NOT affect balances
    balances = await _balances(client, str(house.id), alice)
    assert balances[str(bob.id)] == -3000
    assert balances[str(alice.id)] == 3000

    # payer cannot confirm their own settlement
    r = await client.post(
        f"/api/v1/houses/{house.id}/settlements/{settlement['id']}/confirm",
        headers=auth(bob),
    )
    assert r.status_code == 403

    # payee confirms -> ledger entries written, balances zero out
    r = await client.post(
        f"/api/v1/houses/{house.id}/settlements/{settlement['id']}/confirm",
        headers=auth(alice),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"
    balances = await _balances(client, str(house.id), alice)
    assert balances[str(bob.id)] == 0
    assert balances[str(alice.id)] == 0

    # double confirm -> 409
    r = await client.post(
        f"/api/v1/houses/{house.id}/settlements/{settlement['id']}/confirm",
        headers=auth(alice),
    )
    assert r.status_code == 409


async def test_reject_leaves_balances_untouched(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house, alice, bob = await _setup(session)
    settlement = (
        await client.post(
            f"/api/v1/houses/{house.id}/settlements",
            json={"to_user": str(alice.id), "amount_cents": 3000},
            headers=auth(bob),
        )
    ).json()

    r = await client.post(
        f"/api/v1/houses/{house.id}/settlements/{settlement['id']}/reject",
        headers=auth(bob),
    )
    assert r.status_code == 403  # only payee rejects

    r = await client.post(
        f"/api/v1/houses/{house.id}/settlements/{settlement['id']}/reject",
        headers=auth(alice),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"

    balances = await _balances(client, str(house.id), alice)
    assert balances[str(bob.id)] == -3000  # untouched

    r = await client.post(
        f"/api/v1/houses/{house.id}/settlements/{settlement['id']}/confirm",
        headers=auth(alice),
    )
    assert r.status_code == 409


async def test_settlement_history_lists_pending_and_confirmed(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house, alice, bob = await _setup(session)
    s1 = (
        await client.post(
            f"/api/v1/houses/{house.id}/settlements",
            json={"to_user": str(alice.id), "amount_cents": 1000},
            headers=auth(bob),
        )
    ).json()
    await client.post(
        f"/api/v1/houses/{house.id}/settlements/{s1['id']}/confirm", headers=auth(alice)
    )
    await client.post(
        f"/api/v1/houses/{house.id}/settlements",
        json={"to_user": str(alice.id), "amount_cents": 500},
        headers=auth(bob),
    )

    r = await client.get(f"/api/v1/houses/{house.id}/settlements", headers=auth(alice))
    items = r.json()["items"]
    assert len(items) == 2
    assert {i["status"] for i in items} == {"confirmed", "pending"}


async def test_cross_house_settlement_isolation(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house, alice, bob = await _setup(session)
    stranger = await make_user(session)
    await session.commit()

    r = await client.post(
        f"/api/v1/houses/{house.id}/settlements",
        json={"to_user": str(alice.id), "amount_cents": 100},
        headers=auth(stranger),
    )
    assert r.status_code == 403
