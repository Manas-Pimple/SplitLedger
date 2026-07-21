from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import ApiError
from app.ledger import post_ledger_event
from app.models import Balance, Settlement
from app.models.ledger import LedgerEventKind, SettlementStatus
from app.permissions import AuthContext, Permission, require
from app.settlement_optimizer import suggest_settlement

router = APIRouter(tags=["settlements"])

Session = Annotated[AsyncSession, Depends(get_session)]


class TransferOut(BaseModel):
    from_user: UUID
    to_user: UUID
    amount_cents: int


class SettlementCreateIn(BaseModel):
    to_user: UUID
    amount_cents: int = Field(gt=0)
    method: str | None = None


class SettlementOut(BaseModel):
    id: UUID
    house_id: UUID
    from_user: UUID
    to_user: UUID
    amount_cents: int
    status: SettlementStatus
    method: str | None
    created_at: datetime


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None


def _out(s: Settlement) -> SettlementOut:
    return SettlementOut.model_validate(s, from_attributes=True)


@router.get("/houses/{house_id}/settlements/suggest")
async def suggest(
    ctx: Annotated[AuthContext, Depends(require(Permission.VIEW_LEDGER))],
    session: Session,
) -> list[TransferOut]:
    rows = (
        await session.execute(select(Balance).where(Balance.house_id == ctx.house_id))
    ).scalars()
    balances = {b.user_id: b.balance_cents for b in rows}
    transfers = suggest_settlement(balances)
    return [
        TransferOut(from_user=t.from_user, to_user=t.to_user, amount_cents=t.amount_cents)
        for t in transfers
    ]


@router.post("/houses/{house_id}/settlements", status_code=201)
async def create(
    body: SettlementCreateIn,
    ctx: Annotated[AuthContext, Depends(require(Permission.SETTLEMENT_RECORD))],
    session: Session,
) -> SettlementOut:
    if body.to_user == ctx.principal.user_id:
        raise ApiError(422, "VALIDATION_ERROR", "Cannot settle with yourself")
    settlement = Settlement(
        house_id=ctx.house_id,
        from_user=ctx.principal.user_id,
        to_user=body.to_user,
        amount_cents=body.amount_cents,
        method=body.method,
    )
    session.add(settlement)
    await session.commit()
    # Pending settlements write nothing to the ledger — balances are untouched
    # until the payee confirms (API_SPEC §6).
    return _out(settlement)


async def _get_pending(session: AsyncSession, house_id: UUID, sid: UUID) -> Settlement:
    s = await session.get(Settlement, sid)
    if s is None or s.house_id != house_id:
        raise ApiError(404, "NOT_FOUND", "Settlement not found")
    if s.status != SettlementStatus.pending:
        raise ApiError(409, "CONFLICT", f"Settlement already {s.status}")
    return s


@router.post("/houses/{house_id}/settlements/{sid}/confirm")
async def confirm(
    sid: UUID,
    ctx: Annotated[AuthContext, Depends(require(Permission.SETTLEMENT_RECORD))],
    session: Session,
) -> SettlementOut:
    settlement = await _get_pending(session, ctx.house_id, sid)
    if settlement.to_user != ctx.principal.user_id:
        raise ApiError(403, "PERMISSION_DENIED", "Only the payee can confirm")

    settlement.status = SettlementStatus.confirmed
    await post_ledger_event(
        session,
        house_id=ctx.house_id,
        kind=LedgerEventKind.settlement,
        ref_id=settlement.id,
        entries={
            settlement.from_user: settlement.amount_cents,
            settlement.to_user: -settlement.amount_cents,
        },
        event_type="settlement.confirmed",
        payload={
            "id": str(settlement.id),
            "from_user": str(settlement.from_user),
            "to_user": str(settlement.to_user),
            "amount_cents": settlement.amount_cents,
        },
        created_by=ctx.principal.user_id,
    )
    await session.commit()
    return _out(settlement)


@router.post("/houses/{house_id}/settlements/{sid}/reject")
async def reject(
    sid: UUID,
    ctx: Annotated[AuthContext, Depends(require(Permission.SETTLEMENT_RECORD))],
    session: Session,
) -> SettlementOut:
    settlement = await _get_pending(session, ctx.house_id, sid)
    if settlement.to_user != ctx.principal.user_id:
        raise ApiError(403, "PERMISSION_DENIED", "Only the payee can reject")
    settlement.status = SettlementStatus.rejected
    await session.commit()
    return _out(settlement)


@router.get("/houses/{house_id}/settlements")
async def list_settlements(
    ctx: Annotated[AuthContext, Depends(require(Permission.VIEW_LEDGER))],
    session: Session,
    cursor: UUID | None = None,
    limit: int = 50,
) -> Page[SettlementOut]:
    limit = min(max(limit, 1), 100)
    q = (
        select(Settlement)
        .where(Settlement.house_id == ctx.house_id)
        .order_by(Settlement.id.desc())
        .limit(limit + 1)
    )
    if cursor is not None:
        q = q.where(Settlement.id < cursor)
    rows = list((await session.execute(q)).scalars())
    items = [_out(s) for s in rows[:limit]]
    next_cursor = str(rows[limit - 1].id) if len(rows) > limit else None
    return Page(items=items, next_cursor=next_cursor)
