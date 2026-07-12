"""Ledger engine per ARCHITECTURE.md §4: append-only double-entry, derived
balances behind a row-locked cache, per-house event sequencing, transactional
outbox. post_ledger_event is the single choke point — every ledger write
(expenses, reversals, settlements, adjustments, scheduler bills) goes through
it. Callers own the transaction: this function writes, the caller commits.

Concurrency contract:
- balances rows are upserted then locked with SELECT ... FOR UPDATE in
  deterministic order (sorted by user UUID) — prevents deadlocks.
- the house_seq_counters upsert serialises event ordering per house.
"""

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.models import (
    Balance,
    Event,
    Expense,
    ExpenseShare,
    LedgerEntry,
    LedgerEvent,
    SplitRule,
)
from app.models.expense import ExpenseCategory, ExpenseStatus
from app.models.house import HouseMembership, MembershipStatus
from app.models.ledger import LedgerEventKind
from app.splits import compute_shares

_SEQ_SQL = text("""
    INSERT INTO house_seq_counters (house_id, next_seq) VALUES (:house_id, 1 + :n)
    ON CONFLICT (house_id)
    DO UPDATE SET next_seq = house_seq_counters.next_seq + :n, updated_at = now()
    RETURNING next_seq
""")


async def post_ledger_event(
    session: AsyncSession,
    *,
    house_id: UUID,
    kind: LedgerEventKind,
    ref_id: UUID,
    entries: dict[UUID, int],
    event_type: str,
    payload: dict[str, Any],
    created_by: UUID | None,
) -> LedgerEvent:
    """Write one financial fact: ledger event + entries, locked balance updates,
    and two outbox rows (the domain event + a balance.updated snapshot).
    Does NOT commit — the caller's transaction boundary is the atomicity unit.
    """
    if not entries:
        raise ApiError(422, "VALIDATION_ERROR", "Ledger event needs entries")
    if sum(entries.values()) != 0:
        # the deferred DB trigger would also catch this at commit; fail early
        raise ApiError(422, "VALIDATION_ERROR", "Ledger entries must sum to zero")

    user_ids = sorted(entries)  # deterministic lock order — deadlock prevention

    for uid in user_ids:
        await session.execute(
            text(
                "INSERT INTO balances (house_id, user_id, balance_cents) "
                "VALUES (:h, :u, 0) ON CONFLICT (house_id, user_id) DO NOTHING"
            ),
            {"h": house_id, "u": uid},
        )
    balances = {
        b.user_id: b
        for b in (
            await session.execute(
                select(Balance)
                .where(Balance.house_id == house_id, Balance.user_id.in_(user_ids))
                .order_by(Balance.user_id)
                .with_for_update()
            )
        ).scalars()
    }

    event = LedgerEvent(house_id=house_id, kind=kind, ref_id=ref_id, created_by=created_by)
    session.add(event)
    await session.flush()
    now = datetime.now(UTC)
    for uid in user_ids:
        session.add(
            LedgerEntry(
                ledger_event_id=event.id,
                house_id=house_id,
                user_id=uid,
                amount_cents=entries[uid],
            )
        )
        bal = balances[uid]
        bal.balance_cents += entries[uid]
        if bal.balance_cents >= 0:
            bal.oldest_debt_at = None
        elif bal.oldest_debt_at is None:
            bal.oldest_debt_at = now

    end_seq = (
        await session.execute(_SEQ_SQL, {"house_id": house_id, "n": 2})
    ).scalar_one()
    snapshot = {
        "balances": [
            {
                "user_id": str(b.user_id),
                "balance_cents": b.balance_cents,
                "oldest_debt_at": b.oldest_debt_at.isoformat() if b.oldest_debt_at else None,
            }
            for b in balances.values()
        ]
    }
    session.add_all(
        [
            Event(house_id=house_id, seq=end_seq - 2, type=event_type, payload=payload),
            Event(house_id=house_id, seq=end_seq - 1, type="balance.updated", payload=snapshot),
        ]
    )
    await session.flush()
    return event


def _expense_payload(expense: Expense, shares: dict[UUID, int]) -> dict[str, Any]:
    return {
        "expense": {
            "id": str(expense.id),
            "house_id": str(expense.house_id),
            "description": expense.description,
            "category": expense.category,
            "amount_cents": expense.amount_cents,
            "paid_by": str(expense.paid_by),
            "created_by": str(expense.created_by),
            "status": expense.status,
        },
        "shares": {str(u): c for u, c in shares.items()},
    }


async def create_expense(
    session: AsyncSession,
    *,
    house_id: UUID,
    created_by: UUID,
    paid_by: UUID,
    description: str,
    category: ExpenseCategory,
    amount_cents: int,
    split_rule_id: UUID,
    period: tuple[date, date] | None = None,
    document_id: UUID | None = None,
    recurring_bill_id: UUID | None = None,
) -> Expense:
    """Shared by the API and (Phase 9) the scheduler. Caller commits."""
    rule = await session.get(SplitRule, split_rule_id)
    if rule is None or rule.house_id != house_id:
        raise ApiError(404, "NOT_FOUND", "Split rule not found")

    memberships = (
        await session.execute(
            select(HouseMembership).where(
                HouseMembership.house_id == house_id,
                HouseMembership.status == MembershipStatus.active,
            )
        )
    ).scalars()
    members = {m.user_id: m.away_days for m in memberships}
    shares = compute_shares(rule.kind, rule.config, amount_cents, members, period)

    expense = Expense(
        house_id=house_id,
        created_by=created_by,
        paid_by=paid_by,
        description=description,
        category=category,
        amount_cents=amount_cents,
        split_rule_id=split_rule_id,
        period_start=period[0] if period else None,
        period_end=period[1] if period else None,
        document_id=document_id,
        recurring_bill_id=recurring_bill_id,
    )
    session.add(expense)
    await session.flush()
    session.add_all(
        ExpenseShare(expense_id=expense.id, user_id=u, share_cents=c)
        for u, c in shares.items()
    )

    # Collapsed net-per-user entries (DATA_MODEL §4): payer +amount minus own share
    entries = {u: -c for u, c in shares.items() if u != paid_by}
    entries[paid_by] = amount_cents - shares.get(paid_by, 0)
    entries = {u: c for u, c in entries.items() if c != 0} or {paid_by: 0}

    await post_ledger_event(
        session,
        house_id=house_id,
        kind=LedgerEventKind.expense,
        ref_id=expense.id,
        entries=entries,
        event_type="expense.created",
        payload=_expense_payload(expense, shares),
        created_by=created_by,
    )
    return expense


async def reverse_expense(
    session: AsyncSession, *, expense: Expense, reversed_by: UUID, reason: str
) -> LedgerEvent:
    """Reversal + reissue model: negate the original collapsed entries.
    History is never rewritten. Caller commits."""
    if expense.status == ExpenseStatus.reversed:
        raise ApiError(409, "CONFLICT", "Expense already reversed")

    original = (
        await session.execute(
            select(LedgerEvent).where(
                LedgerEvent.ref_id == expense.id,
                LedgerEvent.kind == LedgerEventKind.expense,
            )
        )
    ).scalar_one()
    rows = (
        await session.execute(
            select(LedgerEntry).where(LedgerEntry.ledger_event_id == original.id)
        )
    ).scalars()
    entries = {r.user_id: -r.amount_cents for r in rows}

    expense.status = ExpenseStatus.reversed
    shares = {
        s.user_id: s.share_cents
        for s in (
            await session.execute(
                select(ExpenseShare).where(ExpenseShare.expense_id == expense.id)
            )
        ).scalars()
    }
    payload = _expense_payload(expense, shares)
    payload["reason"] = reason
    return await post_ledger_event(
        session,
        house_id=expense.house_id,
        kind=LedgerEventKind.expense_reversal,
        ref_id=expense.id,
        entries=entries,
        event_type="expense.reversed",
        payload=payload,
        created_by=reversed_by,
    )
