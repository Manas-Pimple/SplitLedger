from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import ApiError
from app.ledger import emit_events, post_ledger_event, reverse_expense
from app.models import Dispute, DisputeComment, Expense, ExpenseShare
from app.models.dispute import DisputeStatus, ResolutionKind
from app.models.ledger import LedgerEventKind
from app.permissions import AuthContext, Permission, require

router = APIRouter(tags=["disputes"])

Session = Annotated[AsyncSession, Depends(get_session)]


class DisputeCreateIn(BaseModel):
    reason: str = Field(min_length=1)


class CommentIn(BaseModel):
    body: str = Field(min_length=1)


class CommentOut(BaseModel):
    id: UUID
    author_id: UUID
    body: str
    created_at: datetime


class Adjustment(BaseModel):
    user_id: UUID
    amount_cents: int


class ResolveIn(BaseModel):
    kind: ResolutionKind
    adjustments: list[Adjustment] | None = None

    @model_validator(mode="after")
    def adjustment_needs_lines(self) -> "ResolveIn":
        if self.kind == ResolutionKind.adjustment and not self.adjustments:
            raise ValueError("adjustment resolution needs non-empty 'adjustments'")
        if self.kind == ResolutionKind.full_reversal and self.adjustments:
            raise ValueError("full_reversal does not take 'adjustments'")
        return self


class DisputeOut(BaseModel):
    id: UUID
    house_id: UUID
    expense_id: UUID
    opened_by: UUID
    reason: str
    status: DisputeStatus
    resolution_kind: ResolutionKind
    resolution_event_id: UUID | None
    resolved_by: UUID | None
    created_at: datetime


class DisputeDetailOut(DisputeOut):
    comments: list[CommentOut]


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None


def _out(d: Dispute) -> DisputeOut:
    return DisputeOut.model_validate(d, from_attributes=True)


async def _get_dispute(session: AsyncSession, house_id: UUID, did: UUID) -> Dispute:
    d = await session.get(Dispute, did)
    if d is None or d.house_id != house_id:
        raise ApiError(404, "NOT_FOUND", "Dispute not found")
    return d


@router.post("/houses/{house_id}/expenses/{expense_id}/disputes", status_code=201)
async def open_dispute(
    expense_id: UUID,
    body: DisputeCreateIn,
    ctx: Annotated[AuthContext, Depends(require(Permission.DISPUTE_OPEN))],
    session: Session,
) -> DisputeOut:
    expense = await session.get(Expense, expense_id)
    if expense is None or expense.house_id != ctx.house_id:
        raise ApiError(404, "NOT_FOUND", "Expense not found")

    has_share = (
        await session.execute(
            select(ExpenseShare.id).where(
                ExpenseShare.expense_id == expense_id,
                ExpenseShare.user_id == ctx.principal.user_id,
            )
        )
    ).scalar_one_or_none()
    if has_share is None:
        raise ApiError(403, "PERMISSION_DENIED", "Must hold a share in the expense to dispute")

    dispute = Dispute(
        house_id=ctx.house_id,
        expense_id=expense_id,
        opened_by=ctx.principal.user_id,
        reason=body.reason,
    )
    session.add(dispute)
    await session.flush()
    await emit_events(
        session,
        ctx.house_id,
        [
            (
                "dispute.opened",
                {"dispute_id": str(dispute.id), "expense_id": str(expense_id)},
            )
        ],
    )
    await session.commit()
    return _out(dispute)


@router.get("/houses/{house_id}/disputes")
async def list_disputes(
    ctx: Annotated[AuthContext, Depends(require(Permission.VIEW_LEDGER))],
    session: Session,
    status: DisputeStatus | None = None,
    cursor: UUID | None = None,
    limit: int = 50,
) -> Page[DisputeOut]:
    limit = min(max(limit, 1), 100)
    q = (
        select(Dispute)
        .where(Dispute.house_id == ctx.house_id)
        .order_by(Dispute.id.desc())
        .limit(limit + 1)
    )
    if status is not None:
        q = q.where(Dispute.status == status)
    if cursor is not None:
        q = q.where(Dispute.id < cursor)
    rows = list((await session.execute(q)).scalars())
    items = [_out(d) for d in rows[:limit]]
    next_cursor = str(rows[limit - 1].id) if len(rows) > limit else None
    return Page(items=items, next_cursor=next_cursor)


@router.get("/houses/{house_id}/disputes/{did}")
async def get_dispute(
    did: UUID,
    ctx: Annotated[AuthContext, Depends(require(Permission.VIEW_LEDGER))],
    session: Session,
) -> DisputeDetailOut:
    dispute = await _get_dispute(session, ctx.house_id, did)
    comments = (
        await session.execute(
            select(DisputeComment)
            .where(DisputeComment.dispute_id == did)
            .order_by(DisputeComment.created_at)
        )
    ).scalars()
    return DisputeDetailOut(
        **_out(dispute).model_dump(),
        comments=[
            CommentOut(id=c.id, author_id=c.author_id, body=c.body, created_at=c.created_at)
            for c in comments
        ],
    )


@router.post("/houses/{house_id}/disputes/{did}/comments", status_code=201)
async def add_comment(
    did: UUID,
    body: CommentIn,
    ctx: Annotated[AuthContext, Depends(require(Permission.DISPUTE_COMMENT))],
    session: Session,
) -> CommentOut:
    dispute = await _get_dispute(session, ctx.house_id, did)
    comment = DisputeComment(dispute_id=dispute.id, author_id=ctx.principal.user_id, body=body.body)
    session.add(comment)
    await session.flush()
    await emit_events(
        session,
        ctx.house_id,
        [
            (
                "dispute.comment_added",
                {
                    "dispute_id": str(dispute.id),
                    "author_id": str(ctx.principal.user_id),
                    "body": body.body,
                },
            )
        ],
    )
    await session.commit()
    return CommentOut(
        id=comment.id, author_id=comment.author_id, body=comment.body,
        created_at=comment.created_at,
    )


@router.post("/houses/{house_id}/disputes/{did}/review")
async def review(
    did: UUID,
    ctx: Annotated[AuthContext, Depends(require(Permission.DISPUTE_RESOLVE))],
    session: Session,
) -> DisputeOut:
    dispute = await _get_dispute(session, ctx.house_id, did)
    if dispute.status != DisputeStatus.open:
        raise ApiError(409, "CONFLICT", f"Cannot review a dispute that is {dispute.status}")
    dispute.status = DisputeStatus.under_review
    await emit_events(
        session,
        ctx.house_id,
        [("dispute.status_changed", {"dispute_id": str(did), "status": "under_review"})],
    )
    await session.commit()
    return _out(dispute)


@router.post("/houses/{house_id}/disputes/{did}/resolve")
async def resolve(
    did: UUID,
    body: ResolveIn,
    ctx: Annotated[AuthContext, Depends(require(Permission.DISPUTE_RESOLVE))],
    session: Session,
) -> DisputeOut:
    dispute = await _get_dispute(session, ctx.house_id, did)
    if dispute.status != DisputeStatus.under_review:
        raise ApiError(409, "CONFLICT", f"Cannot resolve a dispute that is {dispute.status}")

    expense = await session.get(Expense, dispute.expense_id)
    assert expense is not None
    # Conflict of interest: a manager cannot resolve a dispute they have a stake in.
    if ctx.principal.user_id in (expense.paid_by, dispute.opened_by):
        raise ApiError(
            409,
            "CONFLICT",
            "Conflict of interest — another manager must resolve this dispute",
        )

    if body.kind == ResolutionKind.full_reversal:
        event = await reverse_expense(
            session, expense=expense, reversed_by=ctx.principal.user_id,
            reason=f"dispute {did} resolution",
        )
    else:
        assert body.adjustments is not None
        entries = {a.user_id: a.amount_cents for a in body.adjustments}
        if sum(entries.values()) != 0:
            raise ApiError(422, "VALIDATION_ERROR", "Adjustments must sum to zero")
        event = await post_ledger_event(
            session,
            house_id=ctx.house_id,
            kind=LedgerEventKind.adjustment,
            ref_id=did,
            entries=entries,
            event_type="dispute.resolved",
            payload={
                "dispute_id": str(did),
                "adjustments": {str(u): c for u, c in entries.items()},
            },
            created_by=ctx.principal.user_id,
        )

    dispute.status = DisputeStatus.resolved
    dispute.resolution_kind = body.kind
    dispute.resolution_event_id = event.id
    dispute.resolved_by = ctx.principal.user_id
    await session.commit()
    return _out(dispute)


@router.post("/houses/{house_id}/disputes/{did}/reject")
async def reject(
    did: UUID,
    ctx: Annotated[AuthContext, Depends(require(Permission.DISPUTE_RESOLVE))],
    session: Session,
) -> DisputeOut:
    dispute = await _get_dispute(session, ctx.house_id, did)
    if dispute.status not in (DisputeStatus.open, DisputeStatus.under_review):
        raise ApiError(409, "CONFLICT", f"Cannot reject a dispute that is {dispute.status}")
    dispute.status = DisputeStatus.rejected
    dispute.resolved_by = ctx.principal.user_id
    await emit_events(
        session,
        ctx.house_id,
        [("dispute.status_changed", {"dispute_id": str(did), "status": "rejected"})],
    )
    await session.commit()
    return _out(dispute)
