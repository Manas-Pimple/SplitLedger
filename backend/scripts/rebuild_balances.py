"""Rebuild the balances cache table from ledger_entries.

Integrity proof: balances is a materialised cache; the ledger is the truth.
Reports and repairs any drift. oldest_debt_at is owned by reminder logic and
is not touched here.

Usage: uv run python -m scripts.rebuild_balances
"""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import dispose_engine, get_engine

REBUILD_SQL = text("""
    WITH computed AS (
        SELECT house_id, user_id, SUM(amount_cents) AS balance_cents
        FROM ledger_entries
        GROUP BY house_id, user_id
    ),
    drift AS (
        SELECT
            COALESCE(c.house_id, b.house_id) AS house_id,
            COALESCE(c.user_id, b.user_id) AS user_id,
            COALESCE(c.balance_cents, 0) AS expected,
            COALESCE(b.balance_cents, 0) AS cached
        FROM computed c
        FULL OUTER JOIN balances b
            ON b.house_id = c.house_id AND b.user_id = c.user_id
        WHERE COALESCE(c.balance_cents, 0) IS DISTINCT FROM COALESCE(b.balance_cents, 0)
    ),
    repaired AS (
        INSERT INTO balances (house_id, user_id, balance_cents)
        SELECT house_id, user_id, expected FROM drift
        ON CONFLICT (house_id, user_id)
        DO UPDATE SET balance_cents = EXCLUDED.balance_cents, updated_at = now()
        RETURNING house_id, user_id
    )
    SELECT d.house_id, d.user_id, d.cached, d.expected FROM drift d
""")


async def rebuild(engine: AsyncEngine) -> int:
    async with engine.begin() as conn:
        rows = (await conn.execute(REBUILD_SQL)).fetchall()
    for house_id, user_id, cached, expected in rows:
        print(f"repaired house={house_id} user={user_id}: {cached} -> {expected}")
    print(f"{len(rows)} balance row(s) repaired")
    return len(rows)


async def main() -> None:
    try:
        await rebuild(get_engine())
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
