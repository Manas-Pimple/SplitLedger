"""BUILD_PLAN Phase 9 done-gates:
- monthly bill fires once per period
- catch-up after simulated downtime generates <= 3
- reminder tiers fire once each
- two scheduler processes concurrently produce no duplicates (advisory lock)
"""

import asyncio
from datetime import UTC, datetime, timedelta

from freezegun import freeze_time
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.models import (
    Expense,
    House,
    IdempotencyKey,
    Notification,
    RecurringBill,
    SplitRule,
    User,
)
from app.models.expense import ExpenseCategory
from app.models.house import MembershipRole
from app.models.recurring_bill import BillFrequency
from app.scheduler import _generate_recurring_bills, _send_reminders, tick
from tests.factories import make_house, make_membership, make_split_rule, make_user


async def _make_bill(
    session: AsyncSession,
    house: House,
    payer: User,
    rule: SplitRule,
    next_run_at: datetime,
    **kw: object,
) -> RecurringBill:
    bill = RecurringBill(
        house_id=house.id,
        description="Rent",
        category=ExpenseCategory.rent,
        amount_cents=10000,
        split_rule_id=rule.id,
        paid_by=payer.id,
        frequency=kw.get("frequency", BillFrequency.monthly),
        anchor_day=kw.get("anchor_day", 1),
        next_run_at=next_run_at,
    )
    session.add(bill)
    await session.commit()
    return bill


async def test_monthly_bill_fires_once_per_period(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    house = await make_house(session)
    alice = await make_user(session)
    await make_membership(session, house, alice)
    rule = await make_split_rule(session, house)
    await session.commit()

    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def fired_count() -> int:
        async with factory() as s:
            return len(
                (
                    await s.execute(
                        select(Expense).where(Expense.recurring_bill_id == bill.id)
                    )
                )
                .scalars()
                .all()
            )
    with freeze_time("2026-01-01 00:00:00", real_asyncio=True):
        bill = await _make_bill(
            session, house, alice, rule, datetime(2026, 1, 1, tzinfo=UTC)
        )

    with freeze_time("2026-01-01 00:00:00", real_asyncio=True):
        await _generate_recurring_bills(factory)
    assert await fired_count() == 1

    with freeze_time("2026-01-01 00:00:00", real_asyncio=True):
        # same tick again, still Jan 1: next_run_at already advanced past now -> no-op
        await _generate_recurring_bills(factory)
    assert await fired_count() == 1  # unchanged

    session.expire_all()
    await session.refresh(bill)
    assert bill.next_run_at == datetime(2026, 2, 1, tzinfo=UTC)


async def test_catchup_capped_at_three_periods(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    house = await make_house(session)
    alice = await make_user(session)
    await make_membership(session, house, alice)
    rule = await make_split_rule(session, house)
    await session.commit()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def bills_for(bill_id: object) -> int:
        # Shared test DB (no per-test rollback, see conftest.py): scope by
        # recurring_bill_id rather than trusting the global generated count,
        # since other tests' due bills may share the same tick.
        session.expire_all()
        return len(
            (
                await session.execute(
                    select(Expense).where(Expense.recurring_bill_id == bill_id)
                )
            )
            .scalars()
            .all()
        )

    # bill anchored monthly, server "down" for 5 months
    with freeze_time("2026-01-01 00:00:00", real_asyncio=True):
        bill = await _make_bill(
            session, house, alice, rule, datetime(2026, 1, 1, tzinfo=UTC)
        )

    with freeze_time("2026-06-15 00:00:00", real_asyncio=True):  # 5+ periods behind
        await _generate_recurring_bills(factory)
        assert await bills_for(bill.id) == 3  # capped, not 5

    session.expire_all()
    await session.refresh(bill)
    assert bill.next_run_at == datetime(2026, 4, 1, tzinfo=UTC)  # advanced 3 months

    # next tick catches up the remaining backlog
    with freeze_time("2026-06-15 00:00:00", real_asyncio=True):
        await _generate_recurring_bills(factory)
    assert await bills_for(bill.id) == 6


async def test_paused_bill_does_not_fire(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    house = await make_house(session)
    alice = await make_user(session)
    await make_membership(session, house, alice)
    rule = await make_split_rule(session, house)
    await session.commit()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    with freeze_time("2026-01-01 00:00:00", real_asyncio=True):
        bill = await _make_bill(
            session, house, alice, rule, datetime(2026, 1, 1, tzinfo=UTC)
        )
        bill.is_paused = True
        await session.commit()

    with freeze_time("2026-02-01 00:00:00", real_asyncio=True):
        await _generate_recurring_bills(factory)

    # fresh session: reusing the fixture session's connection after a freeze_time
    # block corrupts the asyncpg/greenlet adapter (observed empirically)
    async with factory() as s:
        fired = (
            await s.execute(select(Expense).where(Expense.recurring_bill_id == bill.id))
        ).scalars().all()
    assert fired == []


async def test_reminder_tiers_fire_once_each(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    house = await make_house(session)
    alice = await make_user(session)  # creditor
    bob = await make_user(session)  # debtor
    await make_membership(session, house, alice, MembershipRole.manager)
    await make_membership(session, house, bob)
    await session.execute(
        text(
            "INSERT INTO balances (house_id, user_id, balance_cents, oldest_debt_at) "
            "VALUES (:h, :b, -1000, :t)"
        ),
        {"h": house.id, "b": bob.id, "t": datetime(2026, 1, 1, tzinfo=UTC)},
    )
    await session.commit()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    with freeze_time("2026-01-08 00:00:00", real_asyncio=True):  # 7 days old -> tier 7
        sent = await _send_reminders(factory)
        assert sent == 1
        sent_again = await _send_reminders(factory)
        assert sent_again == 0  # same tier, no duplicate

    with freeze_time("2026-01-15 00:00:00", real_asyncio=True):  # 14 days old -> tier 14
        sent = await _send_reminders(factory)
        assert sent == 1

    with freeze_time("2026-01-31 00:00:00", real_asyncio=True):  # 30 days old -> tier 30
        sent = await _send_reminders(factory)
        assert sent == 1

    notifications = (
        await session.execute(
            select(Notification).where(
                Notification.user_id == bob.id, Notification.type == "reminder"
            )
        )
    ).scalars().all()
    assert sorted(n.payload["tier"] for n in notifications) == [7, 14, 30]


async def test_idempotency_key_sweep(engine: AsyncEngine, session: AsyncSession) -> None:
    from app.scheduler import _sweep_idempotency_keys

    user = await make_user(session)
    await session.commit()
    session.add(
        IdempotencyKey(
            key="expired-key",
            user_id=user.id,
            request_hash="x",
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    session.add(
        IdempotencyKey(
            key="fresh-key",
            user_id=user.id,
            request_hash="y",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    await session.commit()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    swept = await _sweep_idempotency_keys(factory)
    assert swept >= 1
    remaining = (await session.execute(select(IdempotencyKey.key))).scalars().all()
    assert "expired-key" not in remaining
    assert "fresh-key" in remaining


async def test_concurrent_ticks_no_duplicate_bills(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    house = await make_house(session)
    alice = await make_user(session)
    await make_membership(session, house, alice)
    rule = await make_split_rule(session, house)
    await session.commit()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    with freeze_time("2026-01-01 00:00:00", real_asyncio=True):
        bill = await _make_bill(
            session, house, alice, rule, datetime(2026, 1, 1, tzinfo=UTC)
        )
        results = await asyncio.gather(tick(factory, engine), tick(factory, engine))
    # advisory lock serialises: exactly one ran, the other skipped
    assert sorted(results) == [False, True]

    async with factory() as s:
        count = (
            await s.execute(select(Expense).where(Expense.recurring_bill_id == bill.id))
        ).scalars().all()
    assert len(count) == 1
