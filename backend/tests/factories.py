"""Hand-rolled async factories. Each persists via the given session and
returns the model instance. Money is always integer cents."""

from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.models import (
    Expense,
    ExpenseShare,
    House,
    HouseMembership,
    LedgerEntry,
    LedgerEvent,
    SplitRule,
    User,
)
from app.models.expense import ExpenseCategory
from app.models.house import MembershipRole
from app.models.ledger import LedgerEventKind
from app.models.split_rule import SplitRuleKind


async def make_user(session: AsyncSession, **kw: object) -> User:
    user = User(
        email=kw.get("email", f"user-{uuid4().hex[:12]}@example.com"),
        password_hash=kw.get("password_hash", "x" * 32),
        display_name=kw.get("display_name", "Test User"),
    )
    session.add(user)
    await session.flush()
    return user


async def make_house(session: AsyncSession, **kw: object) -> House:
    house = House(name=kw.get("name", "Test House"))
    session.add(house)
    await session.flush()
    return house


async def make_membership(
    session: AsyncSession,
    house: House,
    user: User,
    role: MembershipRole = MembershipRole.member,
) -> HouseMembership:
    membership = HouseMembership(house_id=house.id, user_id=user.id, role=role)
    session.add(membership)
    await session.flush()
    return membership


async def make_split_rule(session: AsyncSession, house: House, **kw: object) -> SplitRule:
    rule = SplitRule(
        house_id=house.id,
        name=kw.get("name", "Equal split"),
        kind=kw.get("kind", SplitRuleKind.equal),
    )
    session.add(rule)
    await session.flush()
    return rule


async def make_expense(
    session: AsyncSession,
    house: House,
    paid_by: User,
    amount_cents: int = 9000,
    shares: dict[User, int] | None = None,
    **kw: object,
) -> Expense:
    rule = await make_split_rule(session, house)
    expense = Expense(
        house_id=house.id,
        created_by=paid_by.id,
        paid_by=paid_by.id,
        description=kw.get("description", "Test expense"),
        category=kw.get("category", ExpenseCategory.other),
        amount_cents=amount_cents,
        split_rule_id=rule.id,
    )
    session.add(expense)
    await session.flush()
    if shares:
        for user, cents in shares.items():
            session.add(
                ExpenseShare(expense_id=expense.id, user_id=user.id, share_cents=cents)
            )
        await session.flush()
    return expense


async def make_ledger_event(
    session: AsyncSession,
    house: House,
    entries: dict[User, int],
    kind: LedgerEventKind = LedgerEventKind.expense,
) -> LedgerEvent:
    """entries: user -> signed cents. Caller is responsible for zero-sum
    (or deliberately not, to exercise the trigger)."""
    event = LedgerEvent(house_id=house.id, kind=kind, ref_id=uuid7())
    session.add(event)
    await session.flush()
    for user, cents in entries.items():
        session.add(
            LedgerEntry(
                ledger_event_id=event.id,
                house_id=house.id,
                user_id=user.id,
                amount_cents=cents,
            )
        )
    await session.flush()
    return event
