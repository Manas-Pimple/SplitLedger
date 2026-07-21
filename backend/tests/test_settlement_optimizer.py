"""Done-gate: for random balance vectors summing to zero, suggested transfers
<= n-1 and applying them zeroes all balances."""

from uuid import UUID

from hypothesis import given
from hypothesis import strategies as st

from app.settlement_optimizer import suggest_settlement

uuids = st.uuids(version=4)


def _balanced_vectors(draw: st.DrawFn) -> dict[UUID, int]:
    ids = draw(st.lists(uuids, min_size=2, max_size=12, unique=True))
    raw = draw(
        st.lists(
            st.integers(min_value=-100_000, max_value=100_000),
            min_size=len(ids),
            max_size=len(ids),
        )
    )
    # force sum to zero by adjusting the last entry
    raw[-1] -= sum(raw)
    return dict(zip(ids, raw, strict=True))


balanced_vectors = st.composite(_balanced_vectors)()


@given(balances=balanced_vectors)
def test_transfers_bounded_and_zero_out_balances(balances: dict[UUID, int]) -> None:
    transfers = suggest_settlement(balances)
    n = len(balances)
    assert len(transfers) <= max(n - 1, 0)

    result = dict(balances)
    for t in transfers:
        result[t.from_user] += t.amount_cents
        result[t.to_user] -= t.amount_cents
        assert t.amount_cents > 0
    assert all(v == 0 for v in result.values())


def test_deterministic_output_for_same_input() -> None:
    a = UUID("00000000-0000-0000-0000-000000000001")
    b = UUID("00000000-0000-0000-0000-000000000002")
    c = UUID("00000000-0000-0000-0000-000000000003")
    balances = {a: -500, b: -300, c: 800}
    first = suggest_settlement(balances)
    second = suggest_settlement(dict(balances))
    assert first == second


def test_already_settled_yields_no_transfers() -> None:
    a = UUID("00000000-0000-0000-0000-000000000001")
    b = UUID("00000000-0000-0000-0000-000000000002")
    assert suggest_settlement({a: 0, b: 0}) == []


def test_three_way_greedy_matching() -> None:
    alice = UUID("00000000-0000-0000-0000-00000000000a")
    bob = UUID("00000000-0000-0000-0000-00000000000b")
    cara = UUID("00000000-0000-0000-0000-00000000000c")
    # alice owed 1000, bob owed 500, cara owes 1500
    balances = {alice: 1000, bob: 500, cara: -1500}
    transfers = suggest_settlement(balances)
    assert len(transfers) == 2  # n-1 bound
    assert {(t.from_user, t.to_user, t.amount_cents) for t in transfers} == {
        (cara, alice, 1000),
        (cara, bob, 500),
    }
