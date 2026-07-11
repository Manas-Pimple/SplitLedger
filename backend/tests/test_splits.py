"""Done-gate: for random amounts/member counts/rule kinds, shares always sum
exactly to amount_cents (hypothesis property test)."""

from datetime import date
from uuid import UUID

from hypothesis import given
from hypothesis import strategies as st

from app.models.split_rule import SplitRuleKind
from app.splits import compute_shares

uuids = st.uuids(version=4)
amounts = st.integers(min_value=1, max_value=10**9)


Members = dict[UUID, list[dict[str, str]]]


@given(amount=amounts, member_ids=st.lists(uuids, min_size=1, max_size=10, unique=True))
def test_equal_sums_exactly(amount: int, member_ids: list[UUID]) -> None:
    members: Members = {u: [] for u in member_ids}
    shares = compute_shares(SplitRuleKind.equal, {}, amount, members)
    assert sum(shares.values()) == amount
    assert max(shares.values()) - min(shares.values()) <= 1


@given(
    amount=amounts,
    weights=st.dictionaries(
        uuids, st.integers(min_value=1, max_value=1000), min_size=1, max_size=10
    ),
)
def test_weighted_sums_exactly(amount: int, weights: dict[UUID, int]) -> None:
    members: Members = {u: [] for u in weights}
    config = {"weights": {str(u): w for u, w in weights.items()}}
    shares = compute_shares(SplitRuleKind.weighted, config, amount, members)
    assert sum(shares.values()) == amount


@given(
    amount=amounts,
    percents=st.dictionaries(
        uuids, st.floats(min_value=0.1, max_value=100, allow_nan=False), min_size=1, max_size=10
    ),
)
def test_percentage_sums_exactly(amount: int, percents: dict[UUID, float]) -> None:
    members: Members = {u: [] for u in percents}
    config = {"percentages": {str(u): p for u, p in percents.items()}}
    shares = compute_shares(SplitRuleKind.percentage, config, amount, members)
    assert sum(shares.values()) == amount


@given(amount=amounts, member_ids=st.lists(uuids, min_size=1, max_size=6, unique=True))
def test_prorated_sums_exactly(amount: int, member_ids: list[UUID]) -> None:
    members: Members = {}
    for i, u in enumerate(member_ids):
        # stagger away days: member i away first 3*i days of July
        away = (
            [{"start": "2026-07-01", "end": f"2026-07-{3 * i:02d}"}] if 0 < 3 * i <= 31 else []
        )
        members[u] = away
    period = (date(2026, 7, 1), date(2026, 7, 31))
    shares = compute_shares(SplitRuleKind.equal_prorated, {}, amount, members, period)
    assert sum(shares.values()) == amount


def test_remainder_goes_to_lowest_uuids() -> None:
    a = UUID("00000000-0000-0000-0000-000000000001")
    b = UUID("00000000-0000-0000-0000-000000000002")
    c = UUID("00000000-0000-0000-0000-000000000003")
    shares = compute_shares(SplitRuleKind.equal, {}, 100, {a: [], b: [], c: []})
    # 100/3 -> 33 each, remainder 1 cent to lowest UUID
    assert shares == {a: 34, b: 33, c: 33}


def test_prorated_fully_away_member_pays_nothing() -> None:
    home = UUID("00000000-0000-0000-0000-000000000001")
    away = UUID("00000000-0000-0000-0000-000000000002")
    members = {home: [], away: [{"start": "2026-07-01", "end": "2026-07-31"}]}
    shares = compute_shares(
        SplitRuleKind.equal_prorated, {}, 9000, members, (date(2026, 7, 1), date(2026, 7, 31))
    )
    assert shares[away] == 0
    assert shares[home] == 9000
