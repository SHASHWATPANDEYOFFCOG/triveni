"""Which subset of these payments sums to this one bank credit?

This is the question that makes three-way reconciliation hard. A merchant receives one
netted settlement per day, so forty captures on Tuesday arrive as a single credit on
Thursday, minus the MDR, minus 18% GST on that MDR, minus 0.1% TDS, minus refunds that
settled in the same cycle. Matching that credit means choosing a *subset*.

`itertools.combinations` over forty payments is 2^40 and would embarrass us at 5,000
rows, so three algorithms are used in order of cost, and the cheapest one that can
answer is the one that does:

1. **Whole-window first.** In the overwhelmingly common case the answer is "all of
   them" - a settlement is the whole day's captures. Checking that costs one sum, and
   it resolves most days before any search starts.
2. **Meet in the middle.** Exact, O(2^(n/2)) time and space, used up to
   :data:`MITM_LIMIT` items. Amounts are integer paise, so equality is exact and the
   tolerance band is an explicit integer range rather than a float epsilon.
3. **CP-SAT.** Beyond that, an integer program (`ortools`). It is also what makes the
   answer *globally* consistent, in `recon/assign.py`: a payment may belong to at most
   one settlement across the whole day, which per-settlement search cannot guarantee.

Every search reports the size of the space it looked at and how long it took, because
a throughput claim that is not measured is not a claim.

**A tolerance band is not sloppiness.** The band is one-sided and bounded below by
zero: a settlement can only be *less* than the sum of its gross payments, never more,
because every adjustment in the chain is a deduction. Encoding that asymmetry removes
half the search space and rules out a whole class of nonsense match.
"""

from __future__ import annotations

import time
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from core.errors import SolverError

#: Above this many candidates, meet-in-the-middle's 2^(n/2) memory stops being sane
#: (2^17 = 131,072 partial sums per half) and the problem goes to CP-SAT.
MITM_LIMIT: Final = 34

#: Hard ceiling on candidates considered for one target. Beyond this the blocking
#: stage has failed to constrain the problem and the honest answer is to say so
#: rather than to spend a minute proving it.
MAX_CANDIDATES: Final = 400


@dataclass(frozen=True, slots=True)
class SubsetResult:
    """One answer, with the evidence needed to trust or reject it."""

    indices: tuple[int, ...]
    total: int
    """Sum of the chosen items, in paise."""

    target: int
    residual: int
    """``total - target``, in paise. Zero means it balances exactly."""

    method: str
    searched: int
    """How many combinations were actually examined."""

    space: Decimal
    """How many there were in principle - 2^n. The ratio is the pruning story."""

    elapsed_ms: Decimal
    exact: bool = True
    """False when the search was truncated and the answer may not be optimal."""

    @property
    def found(self) -> bool:
        return bool(self.indices)

    def render(self) -> str:
        return (
            f"{len(self.indices)} of {self.space} possible subsets: total {self.total}p "
            f"vs target {self.target}p (residual {self.residual:+d}p) "
            f"via {self.method} in {self.elapsed_ms}ms"
        )


@dataclass(frozen=True, slots=True)
class Tolerance:
    """How far below the gross sum the settlement may land, in paise.

    One-sided on purpose. `lower` is how much may be withheld (fees, GST, TDS,
    reserves); `upper` is normally 0, because money does not appear from nowhere. A
    symmetric band would admit matches where the bank paid more than the merchant
    sold, which is not a reconciliation, it is a bug.
    """

    lower: int
    upper: int = 0

    def __post_init__(self) -> None:
        if self.lower < 0 or self.upper < 0:
            raise SolverError("tolerance bounds are magnitudes and cannot be negative")

    def bounds(self, target: int) -> tuple[int, int]:
        """The inclusive ``[min_total, max_total]`` range of gross sums that explain
        this target.

        One function, because there are three call sites - the whole-window check,
        meet-in-the-middle's binary search, and the CP-SAT constraint - and when they
        each derived the band themselves two of them derived it *inverted*. The solver
        then happily returned settlements where the credit exceeded the gross of the
        payments that produced it by a quarter of a percent: money appearing from
        nowhere, reported as a balanced match.
        """
        return target - self.upper, target + self.lower

    def contains(self, total: int, target: int) -> bool:
        low, high = self.bounds(target)
        return low <= total <= high

    @classmethod
    def from_rate(cls, target: int, *, max_deduction: str = "0.06", slack_paise: int = 100) -> Tolerance:
        """Band sized from the largest plausible total deduction.

        6% comfortably covers the worst case in the Indian rate card: a 4.3%
        international-card MDR, 18% GST on that fee, and 0.1% TDS. ``slack_paise``
        absorbs rounding, since each component is rounded to the paise independently.
        """
        return cls(lower=int(Decimal(target) * Decimal(max_deduction)) + slack_paise, upper=slack_paise)


# --------------------------------------------------------------------------- #
# Whole-window
# --------------------------------------------------------------------------- #
def whole_window(items: Sequence[int], target: int, tolerance: Tolerance) -> SubsetResult | None:
    """Does taking *everything* balance? Usually yes, and it costs one addition.

    A settlement is the whole day's captures unless something unusual happened, so
    trying this before any search resolves most days immediately - and it is also the
    interpretation a human would reach for first, which matters for the reason string.
    """
    started = time.perf_counter()
    total = sum(items)
    if not tolerance.contains(total, target):
        return None
    return SubsetResult(
        indices=tuple(range(len(items))),
        total=total,
        target=target,
        residual=total - target,
        method="whole-window",
        searched=1,
        space=Decimal(2) ** len(items),
        elapsed_ms=Decimal(str(round((time.perf_counter() - started) * 1000, 4))),
    )


# --------------------------------------------------------------------------- #
# Meet in the middle
# --------------------------------------------------------------------------- #
def _half_sums(items: Sequence[int]) -> list[tuple[int, int]]:
    """Every subset sum of a half, as (sum, bitmask). 2^len(items) entries."""
    sums: list[tuple[int, int]] = [(0, 0)]
    for index, value in enumerate(items):
        bit = 1 << index
        sums.extend([(total + value, mask | bit) for total, mask in sums])
    return sums


def meet_in_the_middle(
    items: Sequence[int], target: int, tolerance: Tolerance, *, prefer_largest: bool = True
) -> SubsetResult | None:
    """Exact subset-sum in O(2^(n/2)) rather than O(2^n).

    Split the items in half, enumerate every subset sum of each half, sort one side
    and binary-search it for each sum of the other. For 34 items that is ~131k
    partial sums per half instead of 17 billion combinations.

    ``prefer_largest`` breaks ties toward the subset containing the most payments,
    which is the right prior here: a settlement is normally the whole day, and a
    smaller subset that happens to hit the same total is the more surprising claim.
    """
    started = time.perf_counter()
    n = len(items)
    if n == 0:
        return None
    if n > MITM_LIMIT:
        raise SolverError("too many items for meet-in-the-middle", n=n, limit=MITM_LIMIT)

    split = n // 2
    left = _half_sums(items[:split])
    right = sorted(_half_sums(items[split:]))
    right_sums = [total for total, _mask in right]

    best: tuple[int, int, int] | None = None  # (popcount, left_mask, right_mask)
    searched = 0
    for left_total, left_mask in left:
        min_total, max_total = tolerance.bounds(target)
        start = bisect_left(right_sums, min_total - left_total)
        end = bisect_right(right_sums, max_total - left_total)
        searched += max(end - start, 0)
        for position in range(start, end):
            right_total, right_mask = right[position]
            total = left_total + right_total
            if not tolerance.contains(total, target):
                continue
            popcount = bin(left_mask).count("1") + bin(right_mask).count("1")
            key = popcount if prefer_largest else -popcount
            if best is None or key > best[0]:
                best = (key, left_mask, right_mask)

    elapsed = Decimal(str(round((time.perf_counter() - started) * 1000, 4)))
    if best is None:
        return None

    _key, left_mask, right_mask = best
    indices = [i for i in range(split) if left_mask >> i & 1]
    indices += [split + i for i in range(n - split) if right_mask >> i & 1]
    total = sum(items[i] for i in indices)
    return SubsetResult(
        indices=tuple(indices),
        total=total,
        target=target,
        residual=total - target,
        method="meet-in-the-middle",
        searched=searched,
        space=Decimal(2) ** n,
        elapsed_ms=elapsed,
    )


# --------------------------------------------------------------------------- #
# CP-SAT
# --------------------------------------------------------------------------- #
def cp_sat_subset(
    items: Sequence[int],
    target: int,
    tolerance: Tolerance,
    *,
    weights: Sequence[float] | None = None,
    time_limit_s: float = 5.0,  # money-lint: allow-float solver work units, not an amount
) -> SubsetResult | None:
    """Subset selection as an integer program.

    Used beyond :data:`MITM_LIMIT` items, and it is also the model
    `recon/assign.py` extends to enforce global consistency across a whole day.

    If `ortools` is not installed the caller falls back to meet-in-the-middle with a
    documented, deterministic tie-break; see :func:`solve`.
    """
    from ortools.sat.python import cp_model

    started = time.perf_counter()
    model = cp_model.CpModel()
    chosen = [model.new_bool_var(f"x{i}") for i in range(len(items))]
    total = sum(int(value) * var for value, var in zip(items, chosen, strict=True))
    min_total, max_total = tolerance.bounds(target)
    model.add(total >= min_total)
    model.add(total <= max_total)

    # Maximise matched value, or the supplied weights. Without an objective, CP-SAT
    # returns *a* feasible subset, and "a" is not good enough when several fit: the
    # one containing the most value is the one a human would defend.
    if weights is None:
        model.maximize(total)
    else:
        model.maximize(
            sum(int(w * 1000) * var for w, var in zip(weights, chosen, strict=True))
        )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_workers = 1  # determinism over speed
    solver.parameters.random_seed = 20260101
    status = solver.solve(model)
    elapsed = Decimal(str(round((time.perf_counter() - started) * 1000, 4)))

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None
    indices = tuple(i for i, var in enumerate(chosen) if solver.value(var))
    picked = sum(items[i] for i in indices)
    return SubsetResult(
        indices=indices,
        total=picked,
        target=target,
        residual=picked - target,
        method=f"cp-sat ({solver.status_name(status).lower()})",
        searched=int(solver.num_branches),
        space=Decimal(2) ** len(items),
        elapsed_ms=elapsed,
        exact=status == cp_model.OPTIMAL,
    )


def has_cp_sat() -> bool:
    try:
        from ortools.sat.python import cp_model  # noqa: F401
    except ImportError:
        return False
    return True


# --------------------------------------------------------------------------- #
# The entry point
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class SolveReport:
    """Aggregate statistics across a batch of subset problems, for `make bench`."""

    attempts: int = 0
    solved: int = 0
    by_method: dict[str, int] = field(default_factory=dict)
    total_ms: Decimal = Decimal(0)
    largest_space: Decimal = Decimal(0)

    def record(self, result: SubsetResult | None, method_hint: str = "") -> SolveReport:
        by_method = dict(self.by_method)
        method = result.method if result else f"{method_hint or 'search'}: no solution"
        by_method[method] = by_method.get(method, 0) + 1
        return SolveReport(
            attempts=self.attempts + 1,
            solved=self.solved + (1 if result and result.found else 0),
            by_method=by_method,
            total_ms=self.total_ms + (result.elapsed_ms if result else Decimal(0)),
            largest_space=max(self.largest_space, result.space if result else Decimal(0)),
        )

    def render(self) -> str:
        lines = [
            f"{self.solved}/{self.attempts} subset problems solved in {self.total_ms}ms",
            f"largest search space considered: 2^n = {self.largest_space}",
        ]
        for method, count in sorted(self.by_method.items()):
            lines.append(f"  {count:5} x {method}")
        return "\n".join(lines)


def solve(
    items: Sequence[int],
    target: int,
    tolerance: Tolerance,
    *,
    weights: Sequence[float] | None = None,
    prefer_cp_sat: bool = True,
) -> SubsetResult | None:
    """Cheapest algorithm that can answer, in order.

    Raises rather than guessing when the candidate list is larger than blocking
    should ever have allowed: an unbounded search here would turn a reconciliation
    into a hang, and "the blocker did not constrain this" is a more useful thing to
    tell an operator than a spinner.
    """
    if len(items) > MAX_CANDIDATES:
        raise SolverError(
            "too many candidates for one settlement; blocking failed to constrain it",
            candidates=len(items),
            limit=MAX_CANDIDATES,
        )
    if not items:
        return None

    direct = whole_window(items, target, tolerance)
    if direct is not None:
        return direct

    if len(items) <= MITM_LIMIT:
        return meet_in_the_middle(items, target, tolerance)

    if prefer_cp_sat and has_cp_sat():
        return cp_sat_subset(items, target, tolerance, weights=weights)

    # Documented fallback: keep the largest-value items that fit, deterministically.
    # Not optimal, and it says so through `exact=False` so a caller can route the
    # result to a human instead of posting it.
    started = time.perf_counter()
    order = sorted(range(len(items)), key=lambda i: (-items[i], i))
    _min_total, max_total = tolerance.bounds(target)
    chosen: list[int] = []
    total = 0
    for index in order:
        if total + items[index] <= max_total:
            chosen.append(index)
            total += items[index]
    if not tolerance.contains(total, target):
        return None
    return SubsetResult(
        indices=tuple(sorted(chosen)),
        total=total,
        target=target,
        residual=total - target,
        method="greedy fallback (ortools unavailable)",
        searched=len(items),
        space=Decimal(2) ** len(items),
        elapsed_ms=Decimal(str(round((time.perf_counter() - started) * 1000, 4))),
        exact=False,
    )
