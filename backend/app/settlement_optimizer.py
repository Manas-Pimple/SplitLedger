"""Greedy settlement optimizer per ARCHITECTURE.md §5. Pure function over a
balance snapshot — no side effects, no I/O. Repeatedly matches the largest
debtor with the largest creditor; yields at most n-1 transfers.
"""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class Transfer:
    from_user: UUID
    to_user: UUID
    amount_cents: int


def suggest_settlement(balances: dict[UUID, int]) -> list[Transfer]:
    # Deterministic order: amount desc, user_id asc — stable output for the
    # same input, so a re-run before anyone acts suggests the same transfers.
    debtors = sorted(
        ((u, -b) for u, b in balances.items() if b < 0),
        key=lambda x: (-x[1], x[0]),
    )
    creditors = sorted(
        ((u, b) for u, b in balances.items() if b > 0),
        key=lambda x: (-x[1], x[0]),
    )

    transfers: list[Transfer] = []
    di = ci = 0
    while di < len(debtors) and ci < len(creditors):
        debtor, debt = debtors[di]
        creditor, credit = creditors[ci]
        amount = min(debt, credit)
        transfers.append(Transfer(from_user=debtor, to_user=creditor, amount_cents=amount))
        debt -= amount
        credit -= amount
        debtors[di] = (debtor, debt)
        creditors[ci] = (creditor, credit)
        if debt == 0:
            di += 1
        if credit == 0:
            ci += 1
    return transfers
