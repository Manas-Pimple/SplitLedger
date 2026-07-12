import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.house import MembershipRole
from tests.factories import make_house, make_membership, make_split_rule, make_user
from tests.helpers import auth


async def _setup(session: AsyncSession):  # type: ignore[no-untyped-def]
    house = await make_house(session)
    manager = await make_user(session)
    member = await make_user(session)
    await make_membership(session, house, manager, MembershipRole.manager)
    await make_membership(session, house, member)
    rule = await make_split_rule(session, house)
    await session.commit()
    return house, manager, member, rule


def _body(rule_id: str, amount: int = 9000, **kw: object) -> dict[str, object]:
    return {
        "description": "Groceries",
        "category": "groceries",
        "amount_cents": amount,
        "split_rule_id": rule_id,
        **kw,
    }


async def test_create_expense_moves_balances(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house, manager, member, rule = await _setup(session)

    r = await client.post(
        f"/api/v1/houses/{house.id}/expenses",
        json=_body(str(rule.id)),
        headers=auth(member),
    )
    assert r.status_code == 201, r.text
    detail = r.json()
    assert sum(s["share_cents"] for s in detail["shares"]) == 9000
    assert detail["paid_by"] == str(member.id)

    r = await client.get(f"/api/v1/houses/{house.id}/balances", headers=auth(member))
    balances = {b["user_id"]: b["balance_cents"] for b in r.json()}
    assert balances[str(member.id)] == 4500
    assert balances[str(manager.id)] == -4500


async def test_paid_by_other_requires_manager(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house, manager, member, rule = await _setup(session)

    r = await client.post(
        f"/api/v1/houses/{house.id}/expenses",
        json=_body(str(rule.id), paid_by=str(manager.id)),
        headers=auth(member),
    )
    assert r.status_code == 403

    r = await client.post(
        f"/api/v1/houses/{house.id}/expenses",
        json=_body(str(rule.id), paid_by=str(member.id)),
        headers=auth(manager),
    )
    assert r.status_code == 201
    assert r.json()["paid_by"] == str(member.id)


async def test_reverse_restores_balances_and_guards(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house, manager, member, rule = await _setup(session)

    expense_id = (
        await client.post(
            f"/api/v1/houses/{house.id}/expenses",
            json=_body(str(rule.id)),
            headers=auth(member),
        )
    ).json()["id"]

    # another member (not creator) cannot reverse
    outsider = await make_user(session)
    await make_membership(session, house, outsider)
    await session.commit()
    r = await client.post(
        f"/api/v1/houses/{house.id}/expenses/{expense_id}/reverse",
        json={"reason": "not mine"},
        headers=auth(outsider),
    )
    assert r.status_code == 403

    # creator reverses
    r = await client.post(
        f"/api/v1/houses/{house.id}/expenses/{expense_id}/reverse",
        json={"reason": "wrong amount"},
        headers=auth(member),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "reversed"

    # balances back to zero
    r = await client.get(f"/api/v1/houses/{house.id}/balances", headers=auth(member))
    assert all(b["balance_cents"] == 0 for b in r.json())

    # double reverse → 409
    r = await client.post(
        f"/api/v1/houses/{house.id}/expenses/{expense_id}/reverse",
        json={"reason": "again"},
        headers=auth(manager),
    )
    assert r.status_code == 409


async def test_list_filters_and_cursor(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house, manager, member, rule = await _setup(session)

    for i in range(5):
        r = await client.post(
            f"/api/v1/houses/{house.id}/expenses",
            json=_body(str(rule.id), amount=100 + i),
            headers=auth(member),
        )
        assert r.status_code == 201
    r = await client.post(
        f"/api/v1/houses/{house.id}/expenses",
        json={**_body(str(rule.id)), "category": "rent"},
        headers=auth(manager),
    )
    assert r.status_code == 201

    # newest first, cursor pages of 3: 6 expenses -> 3 + 3
    r = await client.get(
        f"/api/v1/houses/{house.id}/expenses?limit=3", headers=auth(member)
    )
    page1 = r.json()
    assert len(page1["items"]) == 3
    assert page1["next_cursor"] is not None
    r = await client.get(
        f"/api/v1/houses/{house.id}/expenses?limit=3&cursor={page1['next_cursor']}",
        headers=auth(member),
    )
    page2 = r.json()
    assert len(page2["items"]) == 3
    assert page2["next_cursor"] is None
    ids = {e["id"] for e in page1["items"]} | {e["id"] for e in page2["items"]}
    assert len(ids) == 6

    # category filter
    r = await client.get(
        f"/api/v1/houses/{house.id}/expenses?category=rent", headers=auth(member)
    )
    assert [e["category"] for e in r.json()["items"]] == ["rent"]

    # member filter (paid_by)
    r = await client.get(
        f"/api/v1/houses/{house.id}/expenses?member={manager.id}", headers=auth(member)
    )
    assert len(r.json()["items"]) == 1


async def test_ledger_view_shows_events_with_entries(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house, manager, member, rule = await _setup(session)
    await client.post(
        f"/api/v1/houses/{house.id}/expenses",
        json=_body(str(rule.id)),
        headers=auth(member),
    )

    r = await client.get(f"/api/v1/houses/{house.id}/ledger", headers=auth(member))
    assert r.status_code == 200
    events = r.json()["items"]
    assert len(events) == 1
    assert events[0]["kind"] == "expense"
    assert sum(e["amount_cents"] for e in events[0]["entries"]) == 0


async def test_cross_house_expense_access_denied(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house, manager, member, rule = await _setup(session)
    stranger = await make_user(session)
    await session.commit()

    r = await client.get(f"/api/v1/houses/{house.id}/expenses", headers=auth(stranger))
    assert r.status_code == 403
    r = await client.get(f"/api/v1/houses/{house.id}/balances", headers=auth(stranger))
    assert r.status_code == 403
