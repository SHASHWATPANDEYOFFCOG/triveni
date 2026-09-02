"""Stage 4: solve the whole day at once.

Stages 1-3 produce a *scored bipartite graph*, not an answer. The answer is the
maximum-weight matching on that graph, and the difference matters:

Greedy pairwise acceptance takes the highest-scoring pair, then the next, and so on.
It is fast, it is what most reconciliation tools do, and it is wrong in a specific and
expensive way. Suppose payment A scores 9.0 against credit X and 8.8 against credit Y,
while payment B scores 8.9 against X and nothing against Y. Greedy takes A-X, and B is
left unmatched - total 9.0. The optimal assignment is A-Y and B-X, total 17.7. Greedy
does not lose a little precision here; it loses an entire match, and no amount of
better scoring fixes it, because the mistake is in the acceptance rule.

Two layers, because the problem has two shapes:

* **1:1 - min-cost assignment.** Invoices to payments, settlements to bank credits.
  Solved with the Hungarian / Jonker-Volgenant algorithm via
  `scipy.optimize.linear_sum_assignment`. Every row gets a **dummy "no match" column
  priced at the accept threshold**, which is the part worth pausing on: declining to
  match becomes a modelled option with a price, competing on equal terms with every
  real candidate, rather than an afterthought applied afterwards. A row is only
  matched when doing so beats abstaining.

* **Many-to-one - CP-SAT.** Payments to netted settlements. Per-settlement subset-sum
  can find a plausible subset for each credit independently and still produce a
  globally impossible answer, because the same payment can look right in two of them.
  One integer program over the whole day, with "each payment used at most once" as a
  hard constraint, is what makes the result globally consistent instead of locally
  plausible.

Both layers respect the invariants in PART D.1: no row is used twice, and the
conservation identity is checked to the paise before anything is returned.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

import numpy as np
from scipy.optimize import linear_sum_assignment

from core.errors import InfeasibleAssignment, SolverError
from core.money import Money
from recon.subsetsum import Tolerance, has_cp_sat

#: Cost used for a pair that must never be chosen. Large enough to dominate any real
#: score, small enough to keep the cost matrix in a comfortable float range.
FORBIDDEN: Final = 1e6  # money-lint: allow-float match weight in bits or solver units, never an amount


# --------------------------------------------------------------------------- #
# 1:1 assignment
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Assignment:
    """One accepted 1:1 pairing, or an abstention priced against it."""

    left: str
    right: str
    score: float
    """Fellegi-Sunter match weight in bits. Not money."""

    abstained: bool = False


@dataclass(frozen=True, slots=True)
class AssignmentResult:
    pairs: tuple[Assignment, ...]
    abstained: tuple[str, ...]
    total_score: float
    """Sum of match weights in bits. Not money."""

    elapsed_ms: Decimal
    method: str = "linear_sum_assignment"

    def render(self) -> str:
        return (
            f"{len(self.pairs)} pairing(s) worth {self.total_score:.1f} bits, "
            f"{len(self.abstained)} abstention(s), via {self.method} "
            f"in {self.elapsed_ms}ms"
        )


def assign_one_to_one(
    left_ids: Sequence[str],
    right_ids: Sequence[str],
    scores: dict[tuple[str, str], float],
    *,
    no_match_price: float = 0.0,  # money-lint: allow-float match weight in bits, not an amount
) -> AssignmentResult:
    """Maximum-weight 1:1 matching, with abstention as a priced option.

    ``no_match_price`` is the score a row earns by staying unmatched. Setting it to
    the accept threshold means the solver only pairs two rows when the pairing beats
    declining - so the threshold is *inside* the optimisation rather than a filter
    bolted on after it, and the optimum is computed over the real choice set.

    Cost, not score, because `linear_sum_assignment` minimises; the negation is the
    only place that distinction appears.
    """
    started = time.perf_counter()
    if not left_ids or not right_ids:
        return AssignmentResult(
            pairs=(),
            abstained=tuple(sorted([*left_ids, *right_ids])),
            total_score=0.0,  # money-lint: allow-float match weight in bits or solver units, never an amount
            elapsed_ms=Decimal(0),
        )

    n_left, n_right = len(left_ids), len(right_ids)
    # Real candidates on the left block of columns, one dummy no-match column per row
    # on the right block. Every row therefore always has a feasible choice, which is
    # what makes the problem always solvable rather than sometimes infeasible.
    width = n_right + n_left
    cost = np.full((n_left, width), FORBIDDEN, dtype=np.float64)

    for i, left in enumerate(left_ids):
        for j, right in enumerate(right_ids):
            score = scores.get((left, right)) or scores.get((right, left))
            if score is not None:
                cost[i, j] = -score
        cost[i, n_right + i] = -no_match_price

    try:
        rows, columns = linear_sum_assignment(cost)
    except ValueError as exc:  # pragma: no cover - only on a malformed matrix
        raise InfeasibleAssignment("no feasible assignment", left=n_left, right=n_right) from exc

    pairs: list[Assignment] = []
    abstained: list[str] = []
    total = 0.0  # money-lint: allow-float match weight in bits or solver units, never an amount
    for i, j in zip(rows, columns, strict=True):
        if j >= n_right or cost[i, j] >= FORBIDDEN:
            abstained.append(left_ids[i])
            continue
        score = -float(cost[i, j])  # money-lint: allow-float match weight in bits, not an amount
        total += score
        pairs.append(Assignment(left=left_ids[i], right=right_ids[j], score=score))

    matched_right = {p.right for p in pairs}
    abstained.extend(r for r in right_ids if r not in matched_right)

    return AssignmentResult(
        pairs=tuple(sorted(pairs, key=lambda p: (-p.score, p.left))),
        abstained=tuple(sorted(abstained)),
        total_score=total,
        elapsed_ms=Decimal(str(round((time.perf_counter() - started) * 1000, 4))),
    )


# --------------------------------------------------------------------------- #
# Many-to-one assignment
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Bucket:
    """One netted settlement to be explained by a subset of the payments."""

    bucket_id: str
    target: int
    """Net amount that landed, in paise."""

    candidate_ids: tuple[str, ...]
    tolerance: Tolerance


@dataclass(frozen=True, slots=True)
class GroupAssignment:
    bucket_id: str
    member_ids: tuple[str, ...]
    total: int
    residual: int
    exact: bool = True


@dataclass(frozen=True, slots=True)
class ManyToOneResult:
    groups: tuple[GroupAssignment, ...]
    unassigned: tuple[str, ...]
    method: str
    elapsed_ms: Decimal
    branches: int = 0
    optimal: bool = True

    def used_ids(self) -> set[str]:
        return {member for group in self.groups for member in group.member_ids}

    def render(self) -> str:
        return (
            f"{len(self.groups)} settlement group(s), "
            f"{sum(len(g.member_ids) for g in self.groups)} payment(s) attributed, "
            f"{len(self.unassigned)} left over, via {self.method} in {self.elapsed_ms}ms"
        )


def assign_many_to_one(
    buckets: Sequence[Bucket],
    amounts: dict[str, int],
    *,
    weights: dict[tuple[str, str], float] | None = None,
    hint: dict[str, tuple[str, ...]] | None = None,
    attribution_weight: int = 3,
    deterministic_budget: float = 0.5,  # money-lint: allow-float solver work units, not an amount
) -> ManyToOneResult:
    """Attribute payments to netted settlements, globally.

    The constraint that matters is ``sum over buckets of x[payment, bucket] <= 1``.
    Without it, per-settlement subset-sum happily assigns the same payment to Tuesday's
    credit and Thursday's - each locally plausible, jointly impossible. That single
    line is the difference between a demo and a reconciliation.

    ``deterministic_budget`` is measured in the solver's own work units rather than
    seconds, so the same model always stops at the same point on any machine. That is
    the difference between a reproducible reconciliation and one that depends on how
    busy the laptop was.

    ``hint`` supplies a known-good starting assignment - normally "every payment in
    the window belongs to that window's credit", which is what a settlement usually
    is. Without it the solver spends its whole budget proving optimality over a wide
    tolerance band and returns merely FEASIBLE; with it, the same model proves OPTIMAL
    in a fraction of the time. A hint cannot make the answer wrong, only faster: it is
    a starting point, not a constraint.

    Falls back to independent per-bucket search when `ortools` is absent, with the
    result marked ``optimal=False`` so a caller can route it to a human rather than
    post it.
    """
    started = time.perf_counter()
    if not buckets:
        return ManyToOneResult((), (), "no buckets", Decimal(0))

    if not has_cp_sat():
        return _greedy_many_to_one(buckets, amounts, started)

    from ortools.sat.python import cp_model
    from ortools.sat.python.cp_model import IntVar

    model = cp_model.CpModel()
    # Typed rather than `object`: mypy then checks the objective arithmetic, which is
    # where an int/expression mix-up would silently change what is being optimised.
    variables: dict[tuple[str, str], IntVar] = {}
    for bucket in buckets:
        for member in bucket.candidate_ids:
            variables[member, bucket.bucket_id] = model.new_bool_var(f"x_{member}_{bucket.bucket_id}")

    # THE constraint. Each payment belongs to at most one settlement, across the
    # entire day. Without it, per-settlement subset search happily assigns the same
    # payment to Tuesday's credit and Thursday's - each locally plausible, jointly
    # impossible. This single line is the difference between a demo and a
    # reconciliation.
    members = sorted({m for bucket in buckets for m in bucket.candidate_ids})
    for member in members:
        owning = [
            variables[member, bucket.bucket_id]
            for bucket in buckets
            if (member, bucket.bucket_id) in variables
        ]
        if len(owning) > 1:
            model.add_at_most_one(owning)

    # The residual is a SOFT objective, not a hard constraint, and that is the whole
    # modelling decision here.
    #
    # A settlement is not merely gross minus fees. Refunds settle into the same cycle,
    # a chargeback can be held back, a rolling reserve can withhold 5%, and a genuine
    # short-pay is exactly the thing the merchant most needs to see. Insisting the
    # chosen subset hit the credit within a tight band therefore has *no* feasible
    # solution on real data - CP-SAT returned UNKNOWN. Widening the band far enough to
    # admit those cases leaves so many feasible subsets that optimality cannot be
    # proved either.
    #
    # Minimising unexplained money instead is better posed and better modelling: it
    # asks "which attribution leaves the least money unaccounted for?", which is the
    # question a controller actually asks, and it leaves the residual available to
    # Stage 5's decomposition rather than hiding it inside a tolerance.
    penalties = []
    for bucket in buckets:
        total = sum(
            int(amounts[member]) * variables[member, bucket.bucket_id]
            for member in bucket.candidate_ids
        )
        span = sum(abs(int(amounts[m])) for m in bucket.candidate_ids) + abs(bucket.target)

        # A bucket may legitimately receive nothing - a credit nobody can explain is a
        # finding, not an infeasibility. Bounding the residual below by
        # `-tolerance.upper` forbade exactly that, so any credit that could not be
        # satisfied made the *whole day's* model infeasible and the solver returned
        # nothing at all. One unexplainable credit must not take the other nineteen
        # down with it.
        residual = model.new_int_var(-abs(bucket.target) - span, span, f"r_{bucket.bucket_id}")
        model.add(residual == total - bucket.target)

        # "Money does not appear from nowhere" applies only once a bucket is used:
        # given some payments, their gross may not fall meaningfully below the credit.
        bucket_used = model.new_bool_var(f"used_{bucket.bucket_id}")
        model.add_max_equality(
            bucket_used, [variables[m, bucket.bucket_id] for m in bucket.candidate_ids]
        )
        model.add(total >= bucket.target - bucket.tolerance.upper).only_enforce_if(bucket_used)

        # The ABSOLUTE residual, and this is the whole objective.
        #
        # Minimising the signed sum was the first attempt and it is silently useless:
        # once every payment is attributed somewhere, sum(total_b) is fixed and
        # sum(target_b) is a constant, so sum(residual_b) telescopes to a constant and
        # the solver has no preference at all between attributions. It returned an
        # arbitrary consistent one, and 68% of payments landed in the wrong
        # settlement while every hard constraint was satisfied.
        #
        # The absolute value does not telescope: moving a payment into the wrong
        # bucket makes one residual too large and another too small, and both
        # increase the penalty. That is precisely the signal the solver needs.
        penalty = model.new_int_var(0, span, f"abs_r_{bucket.bucket_id}")
        model.add_abs_equality(penalty, residual)
        penalties.append(penalty)

    # Attribute as much as possible, with as little unexplained money as possible.
    # The attribution term is scaled so explaining one more payment always beats
    # shaving a rupee off a residual: a settlement missing a payment is a worse answer
    # than one whose withheld amount is slightly larger than expected.
    # The attribution reward is proportional to each payment's own amount, and the
    # ratio between it and the residual penalty is the precision/recall dial for the
    # whole stage. Writing out the arithmetic for a bucket whose true residual is D:
    #
    #   dropping a payment p < D  changes the objective by  p * (W - 1)
    #   dropping a payment p > D  changes it by             p * (W + 1) - 2D
    #
    # so with W = 1 every payment smaller than the residual is dropped (36% of them
    # were, and recall collapsed), and with W >= 2 essentially everything is kept
    # (recall rises, precision falls). A flat per-payment bonus - the first attempt -
    # is worse than either: set high enough to attribute anything it swamps the
    # residual entirely, and the solver cheerfully attached a Rs 40 payment to a Rs 30
    # credit for a residual of 133%.
    #
    # There is no assumption-free right answer here, which is exactly why the default
    # is chosen by measured rupee cost (scripts/attribution_sweep.py) rather than by
    # feel, and why M12 replaces the choice with a calibrated guarantee.
    attribution = sum(
        int(amounts[member]) * variables[member, bucket.bucket_id]
        for bucket in buckets
        for member in bucket.candidate_ids
    )

    # Per-pair penalties carry the evidence that is not about amounts - principally
    # how far a payment's expected settlement date sits from the day the credit
    # actually landed.
    #
    # Leaving this out was a real modelling failure, not a tuning miss. The settlement
    # calendar was used to *build* the candidate set and then discarded, so the
    # objective saw amounts alone; and because adjacent days' payments are similar in
    # size, swapping them between two credits barely moves any residual. The solver
    # found genuinely low-residual attributions that were nonetheless wrong, and only
    # 40% of payments landed in the right settlement while every constraint held.
    pair_penalty = sum(
        int((weights or {}).get((member, bucket.bucket_id), 0.0))  # money-lint: allow-float match weight in bits or solver units, never an amount
        * variables[member, bucket.bucket_id]
        for bucket in buckets
        for member in bucket.candidate_ids
    )
    # Doubled so the half-weight reward stays in integers, which is what CP-SAT wants.
    model.minimize(sum(penalties) + pair_penalty - attribution_weight * attribution)

    if hint:
        for bucket in buckets:
            wanted = set(hint.get(bucket.bucket_id, ()))
            for member in bucket.candidate_ids:
                model.add_hint(variables[member, bucket.bucket_id], 1 if member in wanted else 0)

    solver = cp_model.CpSolver()
    # A *deterministic* budget, not a wall-clock one. `max_time_in_seconds` makes the
    # answer depend on how fast the machine happened to be that second: two runs of
    # the same reconciliation returned different attributions because the timeout
    # landed in a different place, which breaks the reproducibility the whole project
    # rests on. `max_deterministic_time` is measured in the solver's own work units,
    # so the same model always stops at the same point.
    solver.parameters.max_deterministic_time = deterministic_budget
    solver.parameters.num_workers = 1
    solver.parameters.random_seed = 20260101
    status = solver.solve(model)
    elapsed = Decimal(str(round((time.perf_counter() - started) * 1000, 4)))

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise InfeasibleAssignment(
            "CP-SAT found no consistent attribution",
            status=solver.status_name(status),
            buckets=len(buckets),
        )

    groups: list[GroupAssignment] = []
    for bucket in sorted(buckets, key=lambda b: b.bucket_id):
        chosen = tuple(
            sorted(
                member
                for member in bucket.candidate_ids
                if solver.value(variables[member, bucket.bucket_id])
            )
        )
        if not chosen:
            continue
        # Deliberately a different name from the `total` LinearExpr built above: this
        # one is a plain integer of paise read back out of the solved model, and
        # reusing the name made mypy the only thing noticing they were different types.
        chosen_total = sum(amounts[m] for m in chosen)
        groups.append(
            GroupAssignment(
                bucket_id=bucket.bucket_id,
                member_ids=chosen,
                total=chosen_total,
                residual=chosen_total - bucket.target,
            )
        )

    placed = {m for group in groups for m in group.member_ids}
    return ManyToOneResult(
        groups=tuple(groups),
        unassigned=tuple(sorted(set(members) - placed)),
        method=f"cp-sat ({solver.status_name(status).lower()})",
        elapsed_ms=elapsed,
        branches=int(solver.num_branches),
        optimal=status == cp_model.OPTIMAL,
    )


def _greedy_many_to_one(
    buckets: Sequence[Bucket], amounts: dict[str, int], started: float
) -> ManyToOneResult:
    """Documented fallback: solve each bucket independently, first come first served.

    Not globally optimal and it says so. Buckets are processed in a deterministic
    order so the result is at least reproducible, and every group is marked
    ``exact=False`` so nothing here can be auto-posted without a human.
    """
    from recon.subsetsum import solve as subset_solve

    taken: set[str] = set()
    groups: list[GroupAssignment] = []
    for bucket in sorted(buckets, key=lambda b: (b.bucket_id, -b.target)):
        available = [m for m in bucket.candidate_ids if m not in taken]
        result = subset_solve(
            [amounts[m] for m in available], bucket.target, bucket.tolerance, prefer_cp_sat=False
        )
        if result is None or not result.found:
            continue
        chosen = tuple(sorted(available[i] for i in result.indices))
        taken |= set(chosen)
        groups.append(
            GroupAssignment(
                bucket_id=bucket.bucket_id,
                member_ids=chosen,
                total=result.total,
                residual=result.residual,
                exact=False,
            )
        )
    members = {m for bucket in buckets for m in bucket.candidate_ids}
    return ManyToOneResult(
        groups=tuple(groups),
        unassigned=tuple(sorted(members - taken)),
        method="greedy per-bucket (ortools unavailable)",
        elapsed_ms=Decimal(str(round((time.perf_counter() - started) * 1000, 4))),
        optimal=False,
    )


# --------------------------------------------------------------------------- #
# Invariants
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ConservationCheck:
    """Invariant D.1.2, checked to the paise."""

    matched_gross: Money
    matched_net: Money
    explained: Money
    residual: Money
    holds: bool
    detail: str = ""

    def render(self) -> str:
        mark = "balanced" if self.holds else "OUT OF BALANCE"
        return (
            f"{mark}: gross {self.matched_gross} - explained {self.explained} "
            f"= net {self.matched_net}, residual {self.residual}"
        )


def check_conservation(
    matched_gross: Money, matched_net: Money, explained: Money, *, tolerance: Money | None = None
) -> ConservationCheck:
    """``gross - explained == net``, exactly, unless a tolerance is stated.

    Integer paise throughout, so "exactly" means exactly - there is no epsilon here
    and there should not be. A residual is not noise to be absorbed; it is the
    unexplained money, and it belongs in the exception queue where somebody will
    look at it.
    """
    residual = matched_gross - explained - matched_net
    limit = tolerance or Money.zero(matched_gross.currency)
    holds = abs(residual).paise <= limit.paise
    return ConservationCheck(
        matched_gross=matched_gross,
        matched_net=matched_net,
        explained=explained,
        residual=residual,
        holds=holds,
        detail="" if holds else f"{residual} could not be attributed to any component",
    )


def check_no_double_use(groups: Sequence[GroupAssignment]) -> list[str]:
    """Invariant D.1.3, restated for the many-to-one solver."""
    seen: dict[str, str] = {}
    offenders: list[str] = []
    for group in groups:
        for member in group.member_ids:
            if member in seen:
                offenders.append(f"{member} used by both {seen[member]} and {group.bucket_id}")
            else:
                seen[member] = group.bucket_id
    return offenders


def require_consistent(groups: Sequence[GroupAssignment]) -> None:
    offenders = check_no_double_use(groups)
    if offenders:
        raise SolverError("solver produced a double-booked assignment", offenders=offenders[:5])
