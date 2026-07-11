"""rebuild_balances repairs drift between the balances cache and the ledger."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from scripts.rebuild_balances import rebuild
from tests.factories import make_house, make_ledger_event, make_user


async def test_rebuild_repairs_corrupted_and_missing_rows(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    house = await make_house(session)
    alice = await make_user(session)
    bob = await make_user(session)
    await make_ledger_event(session, house, {alice: 6000, bob: -6000})
    await session.commit()

    # No balance rows exist yet (cache maintenance arrives with Phase 4's
    # post_ledger_event); seed one corrupt row and leave the other missing.
    await session.execute(
        text(
            "INSERT INTO balances (house_id, user_id, balance_cents) "
            "VALUES (:h, :u, 12345)"
        ),
        {"h": house.id, "u": alice.id},
    )
    await session.commit()

    # Shared test DB: other tests' ledger rows may also be repaired; this
    # house contributes exactly two.
    repaired = await rebuild(engine)
    assert repaired >= 2

    rows = (
        await session.execute(
            text(
                "SELECT user_id, balance_cents FROM balances "
                "WHERE house_id = :h ORDER BY balance_cents DESC"
            ),
            {"h": house.id},
        )
    ).fetchall()
    assert [(r.user_id, r.balance_cents) for r in rows] == [
        (alice.id, 6000),
        (bob.id, -6000),
    ]

    # Idempotent: second run repairs nothing.
    assert await rebuild(engine) == 0
