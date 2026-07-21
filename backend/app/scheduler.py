"""Scheduler entrypoint per ARCHITECTURE.md §6. Separate container, same
codebase, different entrypoint (`python -m app.scheduler`) — single instance;
a Postgres advisory lock guards against an accidental second instance rather
than real leader election.

Each tick: acquire the lock (skip tick if held elsewhere) and run, in order:
recurring bill generation, reminder tiers, idempotency-key sweep, orphan
document sweep (stub — R2 delete arrives with Phase 10).
"""

import asyncio
import calendar
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db import get_engine, get_session_factory
from app.ledger import create_expense
from app.models import Balance, Document, House, IdempotencyKey, Notification, RecurringBill
from app.models.document import DocumentStatus
from app.models.recurring_bill import BillFrequency

logger = logging.getLogger(__name__)

TICK_SECONDS = 60
ADVISORY_LOCK_KEY = 84_200_001  # arbitrary fixed key; one scheduler, one lock
MAX_CATCHUP_PERIODS = 3
DEFAULT_REMINDER_DAYS = [7, 14, 30]
IDEMPOTENCY_KEY_TTL_SWEEP = timedelta(hours=24)
ORPHAN_DOCUMENT_AGE = timedelta(hours=24)


def _advance(next_run_at: datetime, frequency: BillFrequency, anchor_day: int) -> datetime:
    if frequency == BillFrequency.weekly:
        return next_run_at + timedelta(days=7)
    if frequency == BillFrequency.fortnightly:
        return next_run_at + timedelta(days=14)
    # monthly: advance one calendar month, clamp day-of-month to the shorter month
    year = next_run_at.year + (next_run_at.month // 12)
    month = next_run_at.month % 12 + 1
    day = min(anchor_day, calendar.monthrange(year, month)[1])
    return next_run_at.replace(year=year, month=month, day=day)


async def _generate_recurring_bills(factory: async_sessionmaker[AsyncSession]) -> int:
    generated = 0
    async with factory() as session:
        due_ids = (
            await session.execute(
                select(RecurringBill.id).where(
                    RecurringBill.next_run_at <= datetime.now(UTC),
                    RecurringBill.is_paused.is_(False),
                )
            )
        ).scalars().all()

    for bill_id in due_ids:
        for _ in range(MAX_CATCHUP_PERIODS):
            async with factory() as session:
                bill = await session.get(RecurringBill, bill_id)
                if bill is None or bill.is_paused or bill.next_run_at > datetime.now(UTC):
                    break
                await create_expense(
                    session,
                    house_id=bill.house_id,
                    created_by=bill.paid_by,
                    paid_by=bill.paid_by,
                    description=bill.description,
                    category=bill.category,
                    amount_cents=bill.amount_cents,
                    split_rule_id=bill.split_rule_id,
                    recurring_bill_id=bill.id,
                    event_type="bill.generated",
                )
                bill.next_run_at = _advance(bill.next_run_at, bill.frequency, bill.anchor_day)
                await session.commit()
                generated += 1
    return generated


async def _send_reminders(factory: async_sessionmaker[AsyncSession]) -> int:
    sent = 0
    now = datetime.now(UTC)
    async with factory() as session:
        debts = (
            await session.execute(
                select(Balance).where(
                    Balance.balance_cents < 0, Balance.oldest_debt_at.is_not(None)
                )
            )
        ).scalars().all()
        houses = {
            h.id: h
            for h in (
                await session.execute(
                    select(House).where(House.id.in_({b.house_id for b in debts}))
                )
            ).scalars()
        }

        for balance in debts:
            house = houses[balance.house_id]
            tiers = house.settings.get("reminder_days", DEFAULT_REMINDER_DAYS)
            assert balance.oldest_debt_at is not None
            age_days = (now - balance.oldest_debt_at).days
            # highest tier reached this run — escalating tone, one notification
            tier = max((t for t in tiers if age_days >= t), default=None)
            if tier is None:
                continue
            debt_key = balance.oldest_debt_at.isoformat()
            already_sent = (
                await session.execute(
                    select(Notification.id).where(
                        Notification.user_id == balance.user_id,
                        Notification.house_id == balance.house_id,
                        Notification.type == "reminder",
                        Notification.payload["tier"].as_integer() == tier,
                        Notification.payload["debt_key"].as_string() == debt_key,
                    )
                )
            ).scalar_one_or_none()
            if already_sent is not None:
                continue

            payload = {
                "tier": tier,
                "debt_key": debt_key,
                "balance_cents": balance.balance_cents,
            }
            session.add(
                Notification(
                    user_id=balance.user_id,
                    house_id=balance.house_id,
                    type="reminder",
                    payload=payload,
                )
            )
            from app.ledger import emit_events

            await emit_events(session, balance.house_id, [("reminder.sent", payload)])
            await session.commit()
            sent += 1
    return sent


async def _sweep_idempotency_keys(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        result = cast(
            CursorResult[Any],
            await session.execute(
                delete(IdempotencyKey).where(IdempotencyKey.expires_at < datetime.now(UTC))
            ),
        )
        await session.commit()
        return result.rowcount or 0


async def _sweep_orphan_documents(factory: async_sessionmaker[AsyncSession]) -> int:
    """ponytail: DB-row cleanup only — R2 object deletion is a Phase 10 stub,
    since no documents exist yet without the upload endpoints."""
    async with factory() as session:
        result = cast(
            CursorResult[Any],
            await session.execute(
                delete(Document).where(
                    Document.status == DocumentStatus.pending,
                    Document.created_at < datetime.now(UTC) - ORPHAN_DOCUMENT_AGE,
                )
            ),
        )
        await session.commit()
        return result.rowcount or 0


async def tick(factory: async_sessionmaker[AsyncSession], engine: AsyncEngine) -> bool:
    """Runs one scheduler pass. Returns False if the advisory lock was held
    elsewhere (tick skipped) — the caller should try again next interval."""
    async with engine.connect() as conn:
        acquired = (
            await conn.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": ADVISORY_LOCK_KEY}
            )
        ).scalar_one()
        if not acquired:
            return False
        try:
            await _generate_recurring_bills(factory)
            await _send_reminders(factory)
            await _sweep_idempotency_keys(factory)
            await _sweep_orphan_documents(factory)
        finally:
            await conn.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": ADVISORY_LOCK_KEY}
            )
    return True


async def run_forever(stop: asyncio.Event | None = None) -> None:
    stop = stop or asyncio.Event()
    engine = get_engine()
    factory = get_session_factory()
    while not stop.is_set():
        try:
            await tick(factory, engine)
        except Exception:
            logger.exception("scheduler tick failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
        except TimeoutError:
            pass


if __name__ == "__main__":
    asyncio.run(run_forever())
