import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.ledger import create_expense
from app.models.expense import ExpenseCategory
from app.models.house import MembershipRole
from app.models.split_rule import SplitRuleKind
from tests.factories import make_house, make_membership, make_split_rule, make_user
from tests.helpers import auth


async def _setup(session: AsyncSession):  # type: ignore[no-untyped-def]
    """alice pays, bob shares the expense; two managers so conflict-of-interest
    always has a legal second resolver to fall back to. Expense goes through the
    real create_expense service so it has a ledger event (reversal needs one)."""
    house = await make_house(session)
    alice = await make_user(session)  # paid_by, member
    bob = await make_user(session)  # share-holder, member
    mgr1 = await make_user(session)  # manager, no stake
    mgr2 = await make_user(session)  # manager, will be conflicted in one test
    await make_membership(session, house, alice)
    await make_membership(session, house, bob)
    await make_membership(session, house, mgr1, MembershipRole.manager)
    await make_membership(session, house, mgr2, MembershipRole.manager)
    rule = await make_split_rule(session, house, kind=SplitRuleKind.equal)
    await session.commit()
    expense = await create_expense(
        session,
        house_id=house.id,
        created_by=alice.id,
        paid_by=alice.id,
        description="split bill",
        category=ExpenseCategory.other,
        amount_cents=1000,
        split_rule_id=rule.id,
    )
    await session.commit()
    return house, alice, bob, mgr1, mgr2, expense


def _open(house_id: object, expense_id: object) -> str:
    return f"/api/v1/houses/{house_id}/expenses/{expense_id}/disputes"


def _d(house_id: object, did: object = "") -> str:
    return f"/api/v1/houses/{house_id}/disputes/{did}".rstrip("/")


async def test_open_requires_share_holder(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house, alice, bob, mgr1, mgr2, expense = await _setup(session)
    outsider = await make_user(session)
    await make_membership(session, house, outsider)
    await session.commit()

    r = await client.post(
        _open(house.id, expense.id), json={"reason": "no share"}, headers=auth(outsider)
    )
    assert r.status_code == 403

    r = await client.post(
        _open(house.id, expense.id), json={"reason": "wrong split"}, headers=auth(bob)
    )
    assert r.status_code == 201
    assert r.json()["status"] == "open"


async def test_comment_visible_to_any_member(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house, alice, bob, mgr1, mgr2, expense = await _setup(session)
    did = (
        await client.post(_open(house.id, expense.id), json={"reason": "x"}, headers=auth(bob))
    ).json()["id"]

    r = await client.post(
        f"{_d(house.id, did)}/comments", json={"body": "looking into it"}, headers=auth(mgr1)
    )
    assert r.status_code == 201

    r = await client.get(_d(house.id, did), headers=auth(alice))
    assert r.status_code == 200
    assert r.json()["comments"][0]["body"] == "looking into it"


@pytest.mark.parametrize(
    ("from_status", "action", "expect_ok"),
    [
        ("open", "review", True),
        ("open", "reject", True),
        ("open", "resolve", False),  # must be reviewed first
        ("under_review", "resolve", True),
        ("under_review", "reject", True),
        ("under_review", "review", False),  # already reviewed
        ("resolved", "review", False),
        ("resolved", "reject", False),
        ("resolved", "resolve", False),
        ("rejected", "review", False),
        ("rejected", "reject", False),
        ("rejected", "resolve", False),
    ],
)
async def test_state_machine_transitions(
    client: httpx.AsyncClient,
    session: AsyncSession,
    from_status: str,
    action: str,
    expect_ok: bool,
) -> None:
    house, alice, bob, mgr1, mgr2, expense = await _setup(session)
    did = (
        await client.post(_open(house.id, expense.id), json={"reason": "x"}, headers=auth(bob))
    ).json()["id"]

    if from_status in ("under_review", "resolved", "rejected"):
        await client.post(f"{_d(house.id, did)}/review", headers=auth(mgr1))
    if from_status == "resolved":
        # resolve with mgr1 (no stake) to reach terminal state cleanly
        zero_adjustments = {
            "kind": "adjustment",
            "adjustments": [
                {"user_id": str(alice.id), "amount_cents": 0},
                {"user_id": str(bob.id), "amount_cents": 0},
            ],
        }
        await client.post(
            f"{_d(house.id, did)}/resolve", json=zero_adjustments, headers=auth(mgr1)
        )
    elif from_status == "rejected":
        await client.post(f"{_d(house.id, did)}/reject", headers=auth(mgr1))

    body = None
    if action == "resolve":
        body = {
            "kind": "adjustment",
            "adjustments": [
                {"user_id": str(alice.id), "amount_cents": -100},
                {"user_id": str(bob.id), "amount_cents": 100},
            ],
        }
    r = await client.post(f"{_d(house.id, did)}/{action}", json=body, headers=auth(mgr1))
    assert (r.status_code < 400) == expect_ok, r.text


async def test_conflict_of_interest_blocks_stakeholder_manager(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house, alice, bob, mgr1, mgr2, expense = await _setup(session)
    # equal split includes all 4 active members, so mgr2 already holds a share
    # in `expense` — opening it themself makes them the disputed party
    did = (
        await client.post(_open(house.id, expense.id), json={"reason": "x"}, headers=auth(mgr2))
    ).json()["id"]
    await client.post(f"{_d(house.id, did)}/review", headers=auth(mgr1))

    # mgr2 is the opener -> conflict of interest
    r = await client.post(
        f"{_d(house.id, did)}/resolve",
        json={"kind": "full_reversal"},
        headers=auth(mgr2),
    )
    assert r.status_code == 409

    # a different manager resolves fine
    r = await client.post(
        f"{_d(house.id, did)}/resolve",
        json={"kind": "full_reversal"},
        headers=auth(mgr1),
    )
    assert r.status_code == 200
    assert r.json()["resolution_kind"] == "full_reversal"


async def test_conflict_of_interest_blocks_paid_by_manager(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house, alice, bob, mgr1, mgr2, expense = await _setup(session)
    # promote alice (paid_by) to manager to create the conflict
    await client.patch(
        f"/api/v1/houses/{house.id}/members/{alice.id}",
        json={"role": "manager"},
        headers=auth(mgr1),
    )
    did = (
        await client.post(_open(house.id, expense.id), json={"reason": "x"}, headers=auth(bob))
    ).json()["id"]
    await client.post(f"{_d(house.id, did)}/review", headers=auth(mgr1))

    r = await client.post(
        f"{_d(house.id, did)}/resolve", json={"kind": "full_reversal"}, headers=auth(alice)
    )
    assert r.status_code == 409


async def test_resolve_adjustment_writes_ledger_and_moves_balances(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house, alice, bob, mgr1, mgr2, expense = await _setup(session)
    did = (
        await client.post(_open(house.id, expense.id), json={"reason": "x"}, headers=auth(bob))
    ).json()["id"]
    await client.post(f"{_d(house.id, did)}/review", headers=auth(mgr1))

    # mismatched sum -> 422
    r = await client.post(
        f"{_d(house.id, did)}/resolve",
        json={
            "kind": "adjustment",
            "adjustments": [
                {"user_id": str(alice.id), "amount_cents": -100},
                {"user_id": str(bob.id), "amount_cents": 50},
            ],
        },
        headers=auth(mgr1),
    )
    assert r.status_code == 422

    r = await client.post(
        f"{_d(house.id, did)}/resolve",
        json={
            "kind": "adjustment",
            "adjustments": [
                {"user_id": str(alice.id), "amount_cents": -100},
                {"user_id": str(bob.id), "amount_cents": 100},
            ],
        },
        headers=auth(mgr1),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "resolved"
    assert body["resolution_kind"] == "adjustment"
    assert body["resolution_event_id"] is not None

    balances = {
        b["user_id"]: b["balance_cents"]
        for b in (
            await client.get(f"/api/v1/houses/{house.id}/balances", headers=auth(mgr1))
        ).json()
    }
    # equal split among 4 members (250 each): alice (paid_by) +750, bob -250;
    # adjustment alice -100 / bob +100 -> 650/-150
    assert balances[str(alice.id)] == 650
    assert balances[str(bob.id)] == -150


async def test_resolve_full_reversal_zeroes_balances(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house, alice, bob, mgr1, mgr2, expense = await _setup(session)
    did = (
        await client.post(_open(house.id, expense.id), json={"reason": "x"}, headers=auth(bob))
    ).json()["id"]
    await client.post(f"{_d(house.id, did)}/review", headers=auth(mgr1))
    r = await client.post(
        f"{_d(house.id, did)}/resolve", json={"kind": "full_reversal"}, headers=auth(mgr1)
    )
    assert r.status_code == 200

    balances = {
        b["user_id"]: b["balance_cents"]
        for b in (
            await client.get(f"/api/v1/houses/{house.id}/balances", headers=auth(mgr1))
        ).json()
    }
    assert balances[str(alice.id)] == 0
    assert balances[str(bob.id)] == 0


async def test_cross_house_dispute_isolation(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    house, alice, bob, mgr1, mgr2, expense = await _setup(session)
    stranger = await make_user(session)
    await session.commit()

    r = await client.post(
        _open(house.id, expense.id), json={"reason": "x"}, headers=auth(stranger)
    )
    assert r.status_code == 403  # not a member at all -> permission dep fails first
