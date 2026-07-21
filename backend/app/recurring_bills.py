"""Minimal CRUD so managers can create rows for the scheduler to act on.

Doc gap (2026-07-21): API_SPEC.md has no recurring-bills endpoint section,
though ROLES_AND_PERMISSIONS.md reserves RECURRING_BILL_MANAGE for managers
and DATA_MODEL.md defines the table. This mirrors the split_rules CRUD
pattern (manage = manager, view = member). Record in DECISIONS.md (Phase 14).
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import ApiError
from app.models import RecurringBill, SplitRule
from app.models.expense import ExpenseCategory
from app.models.recurring_bill import BillFrequency
from app.permissions import AuthContext, Permission, require

router = APIRouter(tags=["recurring-bills"])

Session = Annotated[AsyncSession, Depends(get_session)]


class RecurringBillCreateIn(BaseModel):
    description: str = Field(min_length=1)
    category: ExpenseCategory
    amount_cents: int = Field(gt=0)
    split_rule_id: UUID
    paid_by: UUID
    frequency: BillFrequency
    anchor_day: int = Field(ge=1, le=31)
    next_run_at: datetime


class RecurringBillPatchIn(BaseModel):
    amount_cents: int | None = Field(default=None, gt=0)
    next_run_at: datetime | None = None
    is_paused: bool | None = None


class RecurringBillOut(BaseModel):
    id: UUID
    house_id: UUID
    description: str
    category: ExpenseCategory
    amount_cents: int
    split_rule_id: UUID
    paid_by: UUID
    frequency: BillFrequency
    anchor_day: int
    next_run_at: datetime
    is_paused: bool


def _out(b: RecurringBill) -> RecurringBillOut:
    return RecurringBillOut.model_validate(b, from_attributes=True)


@router.get("/houses/{house_id}/recurring-bills")
async def list_bills(
    ctx: Annotated[AuthContext, Depends(require(Permission.VIEW_LEDGER))],
    session: Session,
) -> list[RecurringBillOut]:
    rows = (
        await session.execute(
            select(RecurringBill).where(RecurringBill.house_id == ctx.house_id)
        )
    ).scalars()
    return [_out(b) for b in rows]


@router.post("/houses/{house_id}/recurring-bills", status_code=201)
async def create_bill(
    body: RecurringBillCreateIn,
    ctx: Annotated[AuthContext, Depends(require(Permission.RECURRING_BILL_MANAGE))],
    session: Session,
) -> RecurringBillOut:
    rule = await session.get(SplitRule, body.split_rule_id)
    if rule is None or rule.house_id != ctx.house_id:
        raise ApiError(404, "NOT_FOUND", "Split rule not found")
    bill = RecurringBill(house_id=ctx.house_id, **body.model_dump())
    session.add(bill)
    await session.commit()
    return _out(bill)


@router.patch("/houses/{house_id}/recurring-bills/{bill_id}")
async def patch_bill(
    bill_id: UUID,
    body: RecurringBillPatchIn,
    ctx: Annotated[AuthContext, Depends(require(Permission.RECURRING_BILL_MANAGE))],
    session: Session,
) -> RecurringBillOut:
    bill = await session.get(RecurringBill, bill_id)
    if bill is None or bill.house_id != ctx.house_id:
        raise ApiError(404, "NOT_FOUND", "Recurring bill not found")
    for field in ("amount_cents", "next_run_at", "is_paused"):
        value = getattr(body, field)
        if value is not None:
            setattr(bill, field, value)
    await session.commit()
    return _out(bill)
