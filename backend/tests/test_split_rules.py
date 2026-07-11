import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SplitRule
from app.models.house import MembershipRole
from tests.factories import make_house, make_membership, make_user
from tests.helpers import auth


async def _setup(session: AsyncSession):  # type: ignore[no-untyped-def]
    house = await make_house(session)
    manager = await make_user(session)
    member = await make_user(session)
    await make_membership(session, house, manager, MembershipRole.manager)
    await make_membership(session, house, member)
    await session.commit()
    return house, manager, member


async def test_rule_crud_roles(client: httpx.AsyncClient, session: AsyncSession) -> None:
    house, manager, member = await _setup(session)

    r = await client.post(
        f"/api/v1/houses/{house.id}/split-rules",
        json={"name": "Equal", "kind": "equal"},
        headers=auth(member),
    )
    assert r.status_code == 403

    r = await client.post(
        f"/api/v1/houses/{house.id}/split-rules",
        json={"name": "Equal", "kind": "equal"},
        headers=auth(manager),
    )
    assert r.status_code == 201
    rule_id = r.json()["id"]

    r = await client.get(f"/api/v1/houses/{house.id}/split-rules", headers=auth(member))
    assert r.status_code == 200
    assert [x["id"] for x in r.json()] == [rule_id]

    # rename + archive allowed
    r = await client.patch(
        f"/api/v1/houses/{house.id}/split-rules/{rule_id}",
        json={"name": "Equal v1", "is_archived": True},
        headers=auth(manager),
    )
    assert r.status_code == 200
    assert r.json()["is_archived"] is True

    # config change via PATCH is not a thing — unknown field rejected by schema? No:
    # pydantic ignores unknown fields by default; assert config is untouched.
    r = await client.patch(
        f"/api/v1/houses/{house.id}/split-rules/{rule_id}",
        json={"config": {"weights": {"x": 1}}},
        headers=auth(manager),
    )
    assert r.status_code == 200
    assert r.json()["config"] == {}


async def test_preview_no_side_effects(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house, manager, member = await _setup(session)

    before = (
        await session.execute(
            select(func.count()).select_from(SplitRule).where(SplitRule.house_id == house.id)
        )
    ).scalar_one()

    r = await client.post(
        f"/api/v1/houses/{house.id}/split-rules/preview",
        json={"kind": "equal", "amount_cents": 9001},
        headers=auth(member),
    )
    assert r.status_code == 200
    shares = r.json()["shares"]
    assert sum(shares.values()) == 9001
    assert len(shares) == 2  # manager + member

    after = (
        await session.execute(
            select(func.count()).select_from(SplitRule).where(SplitRule.house_id == house.id)
        )
    ).scalar_one()
    assert before == after


async def test_preview_weighted_by_rule_id(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house, manager, member = await _setup(session)
    config = {"weights": {str(manager.id): 3, str(member.id): 1}}
    rule_id = (
        await client.post(
            f"/api/v1/houses/{house.id}/split-rules",
            json={"name": "3:1", "kind": "weighted", "config": config},
            headers=auth(manager),
        )
    ).json()["id"]

    r = await client.post(
        f"/api/v1/houses/{house.id}/split-rules/preview",
        json={"rule_id": rule_id, "amount_cents": 8000},
        headers=auth(member),
    )
    assert r.status_code == 200
    shares = r.json()["shares"]
    assert shares[str(manager.id)] == 6000
    assert shares[str(member.id)] == 2000


async def test_preview_bad_config_422(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house, _, member = await _setup(session)
    r = await client.post(
        f"/api/v1/houses/{house.id}/split-rules/preview",
        json={"kind": "weighted", "config": {}, "amount_cents": 100},
        headers=auth(member),
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"