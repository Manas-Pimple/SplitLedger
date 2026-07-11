"""Pure share computation per DATA_MODEL.md §2.

Rounding is doc-mandated deterministic: floor allocation via exact Fraction
arithmetic, then remainder cents distributed one at a time to members ordered
by UUID. Always sums exactly to amount_cents. No floats near money.
"""

from datetime import date
from fractions import Fraction
from typing import Any
from uuid import UUID

from app.errors import ApiError
from app.models.split_rule import SplitRuleKind

# member id -> away-day ranges [{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}, ...]
Members = dict[UUID, list[dict[str, str]]]


def _presence_days(
    away_ranges: list[dict[str, str]], start: date, end: date
) -> int:
    total = (end - start).days + 1
    away = 0
    for r in away_ranges:
        a = max(date.fromisoformat(r["start"]), start)
        b = min(date.fromisoformat(r["end"]), end)
        if a <= b:
            away += (b - a).days + 1
    return max(total - away, 0)


def _weights(
    kind: SplitRuleKind,
    config: dict[str, Any],
    members: Members,
    period: tuple[date, date] | None,
) -> dict[UUID, Fraction]:
    if kind == SplitRuleKind.equal:
        return {u: Fraction(1) for u in members}
    if kind in (SplitRuleKind.percentage, SplitRuleKind.weighted):
        key = "percentages" if kind == SplitRuleKind.percentage else "weights"
        raw = config.get(key)
        if not isinstance(raw, dict) or not raw:
            raise ApiError(422, "VALIDATION_ERROR", f"Rule config needs non-empty '{key}'")
        try:
            weights = {UUID(k): Fraction(str(v)) for k, v in raw.items()}
        except ValueError:
            raise ApiError(422, "VALIDATION_ERROR", f"Invalid '{key}' entry") from None
        if any(w < 0 for w in weights.values()) or sum(weights.values()) <= 0:
            raise ApiError(422, "VALIDATION_ERROR", f"'{key}' must be non-negative, sum > 0")
        unknown = set(weights) - set(members)
        if unknown:
            raise ApiError(422, "VALIDATION_ERROR", f"Unknown members in '{key}': {unknown}")
        return weights
    # equal_prorated: weight = days present in period; degenerate cases -> equal
    if period is None:
        return {u: Fraction(1) for u in members}
    start, end = period
    days = {u: Fraction(_presence_days(away, start, end)) for u, away in members.items()}
    if sum(days.values()) == 0:
        return {u: Fraction(1) for u in members}
    return days


def compute_shares(
    kind: SplitRuleKind,
    config: dict[str, Any],
    amount_cents: int,
    members: Members,
    period: tuple[date, date] | None = None,
) -> dict[UUID, int]:
    if not members:
        raise ApiError(422, "VALIDATION_ERROR", "No members to split between")
    weights = _weights(kind, config, members, period)
    total = sum(weights.values())
    shares = {u: int(amount_cents * w / total) for u, w in weights.items()}  # floor
    remainder = amount_cents - sum(shares.values())
    for u in sorted(weights):  # deterministic: remainder cents by UUID order
        if remainder == 0:
            break
        shares[u] += 1
        remainder -= 1
    return shares
