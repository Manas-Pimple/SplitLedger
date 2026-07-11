from datetime import date
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import ApiError
from app.models import HouseMembership, SplitRule
from app.models.house import MembershipStatus
from app.models.split_rule import SplitRuleKind
from app.permissions import AuthContext, Permission, require
from app.splits import compute_shares

router = APIRouter(tags=["split-rules"])

Session = Annotated[AsyncSession, Depends(get_session)]


class RuleCreateIn(BaseModel):
    name: str = Field(min_length=1)
    kind: SplitRuleKind
    config: dict[str, Any] = {}


class RulePatchIn(BaseModel):
    # archive/rename only — config changes create a new rule (API_SPEC §4)
    name: str | None = Field(default=None, min_length=1)
    is_archived: bool | None = None


class RuleOut(BaseModel):
    id: UUID
    name: str
    kind: SplitRuleKind
    config: dict[str, Any]
    is_archived: bool


class PreviewIn(BaseModel):
    rule_id: UUID | None = None
    kind: SplitRuleKind | None = None
    config: dict[str, Any] | None = None
    amount_cents: int = Field(gt=0)
    period_start: date | None = None
    period_end: date | None = None

    @model_validator(mode="after")
    def rule_or_inline(self) -> "PreviewIn":
        if self.rule_id is None and self.kind is None:
            raise ValueError("Provide rule_id or inline kind+config")
        if (self.period_start is None) != (self.period_end is None):
            raise ValueError("period_start and period_end go together")
        return self


def _rule_out(rule: SplitRule) -> RuleOut:
    return RuleOut(
        id=rule.id, name=rule.name, kind=rule.kind,
        config=rule.config, is_archived=rule.is_archived,
    )


@router.get("/houses/{house_id}/split-rules")
async def list_rules(
    ctx: Annotated[AuthContext, Depends(require(Permission.VIEW_LEDGER))],
    session: Session,
) -> list[RuleOut]:
    rules = (
        await session.execute(select(SplitRule).where(SplitRule.house_id == ctx.house_id))
    ).scalars()
    return [_rule_out(r) for r in rules]


@router.post("/houses/{house_id}/split-rules", status_code=201)
async def create_rule(
    body: RuleCreateIn,
    ctx: Annotated[AuthContext, Depends(require(Permission.SPLIT_RULE_MANAGE))],
    session: Session,
) -> RuleOut:
    rule = SplitRule(
        house_id=ctx.house_id, name=body.name, kind=body.kind, config=body.config
    )
    session.add(rule)
    await session.commit()
    return _rule_out(rule)


@router.patch("/houses/{house_id}/split-rules/{rule_id}")
async def patch_rule(
    rule_id: UUID,
    body: RulePatchIn,
    ctx: Annotated[AuthContext, Depends(require(Permission.SPLIT_RULE_MANAGE))],
    session: Session,
) -> RuleOut:
    rule = await session.get(SplitRule, rule_id)
    if rule is None or rule.house_id != ctx.house_id:
        raise ApiError(404, "NOT_FOUND", "Split rule not found")
    if body.name is not None:
        rule.name = body.name
    if body.is_archived is not None:
        rule.is_archived = body.is_archived
    await session.commit()
    return _rule_out(rule)


@router.post("/houses/{house_id}/split-rules/preview")
async def preview(
    body: PreviewIn,
    ctx: Annotated[AuthContext, Depends(require(Permission.VIEW_LEDGER))],
    session: Session,
) -> dict[str, dict[UUID, int]]:
    if body.rule_id is not None:
        rule = await session.get(SplitRule, body.rule_id)
        if rule is None or rule.house_id != ctx.house_id:
            raise ApiError(404, "NOT_FOUND", "Split rule not found")
        kind, config = rule.kind, rule.config
    else:
        assert body.kind is not None
        kind, config = body.kind, body.config or {}

    memberships = (
        await session.execute(
            select(HouseMembership).where(
                HouseMembership.house_id == ctx.house_id,
                HouseMembership.status == MembershipStatus.active,
            )
        )
    ).scalars()
    members = {m.user_id: m.away_days for m in memberships}
    period = (
        (body.period_start, body.period_end)
        if body.period_start is not None and body.period_end is not None
        else None
    )
    return {"shares": compute_shares(kind, config, body.amount_cents, members, period)}
