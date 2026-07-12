from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import ApiError
from app.ledger import create_expense, reverse_expense
from app.models import Balance, Expense, ExpenseShare, LedgerEntry, LedgerEvent
from app.models.expense import ExpenseCategory, ExpenseStatus
from app.models.house import MembershipRole
from app.models.ledger import LedgerEventKind
from app.permissions import POLICY, AuthContext, Permission, require

router = APIRouter(tags=["expenses"])

Session = Annotated[AsyncSession, Depends(get_session)]


class ExpenseCreateIn(BaseModel):
    description: str = Field(min_length=1)
    category: ExpenseCategory
    amount_cents: int = Field(gt=0)
    split_rule_id: UUID
    paid_by: UUID | None = None  # defaults to caller; others need manager role
    period_start: date | None = None
    period_end: date | None = None
    document_id: UUID | None = None

    @model_validator(mode="after")
    def period_pair(self) -> "ExpenseCreateIn":
        if (self.period_start is None) != (self.period_end is None):
            raise ValueError("period_start and period_end go together")
        return self


class ShareOut(BaseModel):
    user_id: UUID
    share_cents: int


class ExpenseOut(BaseModel):
    id: UUID
    house_id: UUID
    description: str
    category: ExpenseCategory
    amount_cents: int
    paid_by: UUID
    created_by: UUID
    split_rule_id: UUID
    period_start: date | None
    period_end: date | None
    document_id: UUID | None
    status: ExpenseStatus
    created_at: datetime


class ExpenseDetailOut(ExpenseOut):
    shares: list[ShareOut]
    disputes: list[dict[str, str]] = []  # populated in Phase 8


class ReverseIn(BaseModel):
    reason: str = Field(min_length=1)


class BalanceOut(BaseModel):
    user_id: UUID
    balance_cents: int
    oldest_debt_at: datetime | None


class EntryOut(BaseModel):
    user_id: UUID
    amount_cents: int


class LedgerEventOut(BaseModel):
    id: UUID
    kind: LedgerEventKind
    ref_id: UUID
    created_by: UUID | None
    created_at: datetime
    entries: list[EntryOut]


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None


def _expense_out(e: Expense) -> ExpenseOut:
    return ExpenseOut.model_validate(e, from_attributes=True)


@router.post("/houses/{house_id}/expenses", status_code=201)
async def create(
    body: ExpenseCreateIn,
    ctx: Annotated[AuthContext, Depends(require(Permission.EXPENSE_CREATE))],
    session: Session,
) -> ExpenseDetailOut:
    paid_by = body.paid_by or ctx.principal.user_id
    if (
        paid_by != ctx.principal.user_id
        and Permission.EXPENSE_CREATE_ON_BEHALF not in POLICY[ctx.role]
    ):
        raise ApiError(403, "PERMISSION_DENIED", "Manager role required to pay on behalf")

    expense = await create_expense(
        session,
        house_id=ctx.house_id,
        created_by=ctx.principal.user_id,
        paid_by=paid_by,
        description=body.description,
        category=body.category,
        amount_cents=body.amount_cents,
        split_rule_id=body.split_rule_id,
        period=(
            (body.period_start, body.period_end)
            if body.period_start is not None and body.period_end is not None
            else None
        ),
        document_id=body.document_id,
    )
    await session.commit()
    return await _detail(session, expense)


async def _detail(session: AsyncSession, expense: Expense) -> ExpenseDetailOut:
    shares = (
        await session.execute(
            select(ExpenseShare).where(ExpenseShare.expense_id == expense.id)
        )
    ).scalars()
    return ExpenseDetailOut(
        **_expense_out(expense).model_dump(),
        shares=[ShareOut(user_id=s.user_id, share_cents=s.share_cents) for s in shares],
    )


@router.get("/houses/{house_id}/expenses")
async def list_expenses(
    ctx: Annotated[AuthContext, Depends(require(Permission.VIEW_LEDGER))],
    session: Session,
    category: ExpenseCategory | None = None,
    member: UUID | None = None,
    cursor: UUID | None = None,
    limit: int = 50,
) -> Page[ExpenseOut]:
    limit = min(max(limit, 1), 100)
    q = (
        select(Expense)
        .where(Expense.house_id == ctx.house_id)
        .order_by(Expense.id.desc())
        .limit(limit + 1)
    )
    if category is not None:
        q = q.where(Expense.category == category)
    if member is not None:
        q = q.where(Expense.paid_by == member)
    if cursor is not None:
        q = q.where(Expense.id < cursor)  # UUIDv7: time-ordered, so id is the cursor
    rows = list((await session.execute(q)).scalars())
    items = [_expense_out(e) for e in rows[:limit]]
    next_cursor = str(rows[limit - 1].id) if len(rows) > limit else None
    return Page(items=items, next_cursor=next_cursor)


@router.get("/houses/{house_id}/expenses/{expense_id}")
async def get_expense(
    expense_id: UUID,
    ctx: Annotated[AuthContext, Depends(require(Permission.VIEW_LEDGER))],
    session: Session,
) -> ExpenseDetailOut:
    expense = await session.get(Expense, expense_id)
    if expense is None or expense.house_id != ctx.house_id:
        raise ApiError(404, "NOT_FOUND", "Expense not found")
    return await _detail(session, expense)


@router.post("/houses/{house_id}/expenses/{expense_id}/reverse")
async def reverse(
    expense_id: UUID,
    body: ReverseIn,
    ctx: Annotated[AuthContext, Depends(require(Permission.EXPENSE_REVERSE))],
    session: Session,
) -> ExpenseDetailOut:
    expense = await session.get(Expense, expense_id)
    if expense is None or expense.house_id != ctx.house_id:
        raise ApiError(404, "NOT_FOUND", "Expense not found")
    # object-level rule: members reverse their own expenses only
    if ctx.role != MembershipRole.manager and expense.created_by != ctx.principal.user_id:
        raise ApiError(403, "PERMISSION_DENIED", "Only the creator or a manager can reverse")

    await reverse_expense(
        session, expense=expense, reversed_by=ctx.principal.user_id, reason=body.reason
    )
    await session.commit()
    return await _detail(session, expense)


@router.get("/houses/{house_id}/balances")
async def balances(
    ctx: Annotated[AuthContext, Depends(require(Permission.VIEW_LEDGER))],
    session: Session,
) -> list[BalanceOut]:
    rows = (
        await session.execute(select(Balance).where(Balance.house_id == ctx.house_id))
    ).scalars()
    return [
        BalanceOut(
            user_id=b.user_id, balance_cents=b.balance_cents, oldest_debt_at=b.oldest_debt_at
        )
        for b in rows
    ]


@router.get("/houses/{house_id}/ledger")
async def ledger(
    ctx: Annotated[AuthContext, Depends(require(Permission.VIEW_LEDGER))],
    session: Session,
    cursor: UUID | None = None,
    limit: int = 50,
) -> Page[LedgerEventOut]:
    limit = min(max(limit, 1), 100)
    q = (
        select(LedgerEvent)
        .where(LedgerEvent.house_id == ctx.house_id)
        .order_by(LedgerEvent.id.desc())
        .limit(limit + 1)
    )
    if cursor is not None:
        q = q.where(LedgerEvent.id < cursor)
    events = list((await session.execute(q)).scalars())
    page = events[:limit]
    entries_by_event: dict[UUID, list[EntryOut]] = {e.id: [] for e in page}
    if page:
        rows = (
            await session.execute(
                select(LedgerEntry).where(
                    LedgerEntry.ledger_event_id.in_(entries_by_event)
                )
            )
        ).scalars()
        for r in rows:
            entries_by_event[r.ledger_event_id].append(
                EntryOut(user_id=r.user_id, amount_cents=r.amount_cents)
            )
    items = [
        LedgerEventOut(
            id=e.id, kind=e.kind, ref_id=e.ref_id, created_by=e.created_by,
            created_at=e.created_at, entries=entries_by_event[e.id],
        )
        for e in page
    ]
    next_cursor = str(events[limit - 1].id) if len(events) > limit else None
    return Page(items=items, next_cursor=next_cursor)
