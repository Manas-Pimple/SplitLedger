"""Ledger engine core + the Phase 4 done-gate concurrency test."""

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from uuid6 import uuid7

from app.errors import ApiError
from app.ledger import create_expense, post_ledger_event
from app.models import Balance, Event
from app.models.expense import ExpenseCategory
from app.models.ledger import LedgerEventKind
from scripts.rebuild_balances import rebuild
from tests.factories import make_house, make_membership, make_split_rule, make_user


async def test_post_event_updates_balances_and_outbox(session: AsyncSession) -> None:
    house = await make_house(session)
    alice = await make_user(session)
    bob = await make_user(session)
    await session.commit()

    await post_ledger_event(
        session,
        house_id=house.id,
        kind=LedgerEventKind.expense,
        ref_id=uuid7(),
        entries={alice.id: 6000, bob.id: -6000},
        event_type="expense.created",
        payload={"test": "1"},
        created_by=alice.id,
    )
    await session.commit()

    balances = {
        b.user_id: b
        for b in (
            await session.execute(select(Balance).where(Balance.house_id == house.id))
        ).scalars()
    }
    assert balances[alice.id].balance_cents == 6000
    assert balances[alice.id].oldest_debt_at is None
    assert balances[bob.id].balance_cents == -6000
    assert balances[bob.id].oldest_debt_at is not None

    events = list(
        (
            await session.execute(
                select(Event).where(Event.house_id == house.id).order_by(Event.seq)
            )
        ).scalars()
    )
    assert [e.type for e in events] == ["expense.created", "balance.updated"]
    assert [e.seq for e in events] == [1, 2]
    assert events[1].payload["balances"][0]["balance_cents"] in (6000, -6000)
    assert all(e.published_at is None for e in events)  # outbox: pending relay


async def test_non_zero_sum_rejected_before_db(session: AsyncSession) -> None:
    house = await make_house(session)
    alice = await make_user(session)
    await session.commit()
    with pytest.raises(ApiError) as e:
        await post_ledger_event(
            session,
            house_id=house.id,
            kind=LedgerEventKind.expense,
            ref_id=uuid7(),
            entries={alice.id: 1},
            event_type="x",
            payload={},
            created_by=None,
        )
    assert e.value.status == 422


async def test_debt_clears_oldest_debt_at(session: AsyncSession) -> None:
    house = await make_house(session)
    alice = await make_user(session)
    bob = await make_user(session)
    await session.commit()

    await post_ledger_event(
        session, house_id=house.id, kind=LedgerEventKind.expense, ref_id=uuid7(),
        entries={alice.id: 500, bob.id: -500}, event_type="expense.created",
        payload={}, created_by=None,
    )
    await session.commit()
    await post_ledger_event(
        session, house_id=house.id, kind=LedgerEventKind.settlement, ref_id=uuid7(),
        entries={alice.id: -500, bob.id: 500}, event_type="settlement.confirmed",
        payload={}, created_by=None,
    )
    await session.commit()

    bob_balance = (
        await session.execute(
            select(Balance).where(Balance.house_id == house.id, Balance.user_id == bob.id)
        )
    ).scalar_one()
    assert bob_balance.balance_cents == 0
    assert bob_balance.oldest_debt_at is None


async def test_concurrent_expense_creation(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    """Done-gate: 20 consecutive rounds of parallel expense creation against one
    house; final balances match the serial computation; cache matches rebuild."""
    house = await make_house(session)
    users = [await make_user(session) for _ in range(4)]
    for u in users:
        await make_membership(session, house, u)
    rule = await make_split_rule(session, house)
    await session.commit()
    house_id, rule_id = house.id, rule.id
    user_ids = [u.id for u in users]

    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def one_expense(payer_idx: int, amount: int) -> None:
        async with factory() as s:
            await create_expense(
                s,
                house_id=house_id,
                created_by=user_ids[payer_idx],
                paid_by=user_ids[payer_idx],
                description=f"parallel {amount}",
                category=ExpenseCategory.other,
                amount_cents=amount,
                split_rule_id=rule_id,
            )
            await s.commit()

    parallel = 10
    rounds = 20
    for r in range(rounds):
        await asyncio.gather(
            *(one_expense(i % 4, 1000 + r * parallel + i) for i in range(parallel))
        )

    # serial expectation: replay every expense through compute_shares logic
    from app.models.split_rule import SplitRuleKind
    from app.splits import compute_shares

    expected = dict.fromkeys(user_ids, 0)
    for r in range(rounds):
        for i in range(parallel):
            amount = 1000 + r * parallel + i
            payer = user_ids[i % 4]
            shares = compute_shares(
                SplitRuleKind.equal, {}, amount, {u: [] for u in user_ids}
            )
            for uid, c in shares.items():
                expected[uid] -= c
            expected[payer] += amount

    session.expire_all()
    actual = {
        b.user_id: b.balance_cents
        for b in (
            await session.execute(select(Balance).where(Balance.house_id == house_id))
        ).scalars()
    }
    assert actual == expected

    # cache provably rebuildable from the ledger: rebuild leaves this house's
    # rows untouched (other tests seed unbacked balance rows, so no global count)
    await rebuild(engine)
    session.expire_all()
    rebuilt = {
        b.user_id: b.balance_cents
        for b in (
            await session.execute(select(Balance).where(Balance.house_id == house_id))
        ).scalars()
    }
    assert rebuilt == expected

    # event seqs are gapless and strictly ordered per house
    seqs = [
        e.seq
        for e in (
            await session.execute(
                select(Event).where(Event.house_id == house_id).order_by(Event.seq)
            )
        ).scalars()
    ]
    assert seqs == list(range(1, len(seqs) + 1))


async def test_create_expense_snapshot_and_rule_check(session: AsyncSession) -> None:
    house = await make_house(session)
    other_house = await make_house(session)
    alice = await make_user(session)
    await make_membership(session, house, alice)
    foreign_rule = await make_split_rule(session, other_house)
    await session.commit()

    with pytest.raises(ApiError) as e:
        await create_expense(
            session,
            house_id=house.id,
            created_by=alice.id,
            paid_by=alice.id,
            description="x",
            category=ExpenseCategory.other,
            amount_cents=100,
            split_rule_id=foreign_rule.id,  # rule from another house
        )
    assert e.value.status == 404
