"""DB-level integrity triggers: zero-sum ledger events, share-sum expenses.
Both are deferred constraint triggers — violations surface at COMMIT."""

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ExpenseShare
from tests.factories import (
    make_expense,
    make_house,
    make_ledger_event,
    make_user,
)


async def test_zero_sum_ledger_event_commits(session: AsyncSession) -> None:
    house = await make_house(session)
    alice = await make_user(session)
    bob = await make_user(session)
    await make_ledger_event(session, house, {alice: 6000, bob: -6000})
    await session.commit()


async def test_non_zero_sum_ledger_event_rejected(session: AsyncSession) -> None:
    house = await make_house(session)
    alice = await make_user(session)
    bob = await make_user(session)
    await make_ledger_event(session, house, {alice: 6000, bob: -5999})
    with pytest.raises(DBAPIError, match="sum to 1"):
        await session.commit()
    await session.rollback()


async def test_matching_shares_commit(session: AsyncSession) -> None:
    house = await make_house(session)
    alice = await make_user(session)
    bob = await make_user(session)
    await make_expense(
        session, house, alice, amount_cents=9000, shares={alice: 4500, bob: 4500}
    )
    await session.commit()


async def test_mismatched_shares_rejected(session: AsyncSession) -> None:
    house = await make_house(session)
    alice = await make_user(session)
    bob = await make_user(session)
    await make_expense(
        session, house, alice, amount_cents=9000, shares={alice: 4500, bob: 4000}
    )
    with pytest.raises(DBAPIError, match="shares sum to 8500"):
        await session.commit()
    await session.rollback()


async def test_partial_share_delete_rejected(session: AsyncSession) -> None:
    house = await make_house(session)
    alice = await make_user(session)
    bob = await make_user(session)
    expense = await make_expense(
        session, house, alice, amount_cents=9000, shares={alice: 4500, bob: 4500}
    )
    await session.commit()

    share = await session.get(
        ExpenseShare, (await _first_share_id(session, expense.id))
    )
    assert share is not None
    await session.delete(share)
    with pytest.raises(DBAPIError):
        await session.commit()
    await session.rollback()


async def _first_share_id(session: AsyncSession, expense_id: object) -> object:
    from sqlalchemy import select

    result = await session.execute(
        select(ExpenseShare.id).where(ExpenseShare.expense_id == expense_id).limit(1)
    )
    return result.scalar_one()
