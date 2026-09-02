"""Stage 3: probabilistic record linkage, Fellegi-Sunter, fitted by EM.

Blocking said which pairs are worth comparing. This stage says *how much* each
comparison is worth, and - the part that matters for a finance team - **why**.

The model is Fellegi & Sunter's (JASA, 1969). For each field we define a small set of
agreement levels (exact / close / different) and estimate two probabilities:

    m_f(l) = P(field f shows level l | the pair IS a match)
    u_f(l) = P(field f shows level l | the pair is NOT a match)

The evidence a field contributes is the log-odds of those two, and because the fields
are treated as conditionally independent given match status, the total score is their
sum:

    w_f(l) = log2( m_f(l) / u_f(l) )      score(pair) = sum over f of w_f(level_f)

Two things follow, and both are the reason this is not just a similarity score.

**It is calibrated to the data, not to a guess.** ``m`` and ``u`` are not hand-set;
they are fitted by expectation-maximisation over the candidate pairs themselves, with
no labels. A UTR agreeing is worth a lot precisely *because* u(UTR agrees) is tiny -
random pairs almost never share a reference - and EM discovers that from the data
rather than being told.

**It is explainable per field.** A total score of 14.2 tells a human nothing. "UTR
agrees: +11.3 · amount agrees exactly: +4.1 · counterparty differs: -1.2" tells them
everything, and that breakdown is what the triage UI renders as a diverging bar chart.

**The assumption, stated.** Conditional independence given match status is the classic
Fellegi-Sunter simplification and it is not exactly true here - amount agreement and
reference agreement are correlated, because both are consequences of being the same
payment. The effect is over-confident scores at the extremes. We mitigate it by
letting the conformal calibrator at M12 map scores to a *guaranteed* error rate rather
than trusting the probabilities directly, which is the honest way to use a model whose
assumptions you know to be approximate. Splink (Linacre et al., IJPDS 2022) is the
modern scalable implementation of the same theory; we implement it here to own the
per-field weight breakdown end to end.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import IntEnum
from typing import Final

from core.clock import INDIAN_BANK_CALENDAR, SettlementCalendar
from ingest.canonical import CanonicalTxn, SourceKind, TxnKind
from recon.blocking import Pair, embed
from recon.normalize import counterparty_tokens, name_key

#: Laplace smoothing on the M-step. Without it, a level that never co-occurs with a
#: match drives m to zero and the weight to -infinity, and one unlucky field silently
#: vetoes every pair.
SMOOTHING: Final = 0.001

#: Floor and ceiling on any single field's weight, in bits. A field is evidence, not a
#: verdict; nothing should be able to decide a match single-handed.
WEIGHT_CLAMP: Final = 12.0


class Level(IntEnum):
    """Agreement levels, coarse on purpose.

    Three or four levels per field is the sweet spot: enough to separate "identical"
    from "similar" from "unrelated", few enough that EM has data to estimate each one.
    """

    DISAGREE = 0
    WEAK = 1
    STRONG = 2
    EXACT = 3
    MISSING = 4
    """Neither side had the field. Carries no evidence either way, by construction."""


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One comparison field: how to level it, and how to describe the result."""

    name: str
    levels: int
    describe: dict[Level, str]


FIELDS: Final[tuple[FieldSpec, ...]] = (
    FieldSpec(
        "reference",
        4,
        {
            Level.EXACT: "UTR / reference identical",
            Level.STRONG: "one reference contains the other",
            Level.WEAK: "references share a long digit run",
            Level.DISAGREE: "references differ",
            Level.MISSING: "no reference on one side",
        },
    ),
    FieldSpec(
        "amount",
        4,
        {
            Level.EXACT: "amounts identical to the paise",
            Level.STRONG: "within 3% - consistent with fees withheld",
            Level.WEAK: "within 25% - consistent with a netted settlement",
            Level.DISAGREE: "amounts unrelated",
            Level.MISSING: "amount missing",
        },
    ),
    FieldSpec(
        "date",
        4,
        {
            Level.EXACT: "lands on the expected settlement date",
            Level.STRONG: "within the expected settlement window",
            Level.WEAK: "within a week",
            Level.DISAGREE: "outside any plausible window",
            Level.MISSING: "date missing",
        },
    ),
    FieldSpec(
        "counterparty",
        4,
        {
            Level.EXACT: "same normalised counterparty",
            Level.STRONG: "same phonetic key",
            Level.WEAK: "share a name token",
            Level.DISAGREE: "different counterparties",
            Level.MISSING: "counterparty missing on one side",
        },
    ),
    FieldSpec(
        "narration",
        3,
        {
            Level.STRONG: "narration text closely similar",
            Level.WEAK: "narration text loosely similar",
            Level.DISAGREE: "narration text unrelated",
            Level.MISSING: "no narration",
        },
    ),
)

FIELD_NAMES: Final[tuple[str, ...]] = tuple(f.name for f in FIELDS)


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #
def _reference_level(left: CanonicalTxn, right: CanonicalTxn) -> Level:
    a = left.reference or str(left.metadata.get("utr") or "")
    b = right.reference or str(right.metadata.get("utr") or "")
    if not a or not b:
        return Level.MISSING
    if a == b:
        return Level.EXACT
    if a in b or b in a:
        return Level.STRONG
    # A shared run of nine or more digits is a reference in common, however the two
    # systems decorated it.
    for size in range(min(len(a), len(b)), 8, -1):
        if any(a[i : i + size] in b for i in range(len(a) - size + 1)):
            return Level.WEAK
    return Level.DISAGREE


def _amount_level(left: CanonicalTxn, right: CanonicalTxn) -> Level:
    if left.amount.currency != right.amount.currency:
        return Level.DISAGREE
    a, b = abs(left.amount.paise), abs(right.amount.paise)
    if a == 0 or b == 0:
        return Level.MISSING
    if a == b:
        return Level.EXACT
    ratio = min(a, b) / max(a, b)
    if ratio >= 0.97:
        # Consistent with MDR + GST + TDS having been withheld.
        return Level.STRONG
    if ratio >= 0.75:
        return Level.WEAK
    return Level.DISAGREE


def _date_level(
    left: CanonicalTxn,
    right: CanonicalTxn,
    *,
    calendar: SettlementCalendar = INDIAN_BANK_CALENDAR,
    cycle_days: int = 2,
) -> Level:
    """Dates compared through the settlement calendar - and through the *relation*.

    Which comparison is correct depends entirely on what the two rows are to each
    other, and getting this wrong is subtle because the model still fits, it just
    learns nonsense:

    * **payment to invoice** - both are dated on the day of the sale, so they should
      simply agree. Projecting T+2 here made every true pair land two days from
      "expected", so EM dutifully learned that a two-day gap indicates a match and
      that landing on the expected settlement date indicates a non-match. The weights
      were inverted, and only reading the fitted table revealed it.
    * **payment or invoice to a bank credit** - genuinely T+2 across the Indian bank
      calendar; this is the projection the whole settlement model rests on.
    * **settlement record to a bank credit** - both already dated at the settlement.
    """
    left_is_settlement = left.source is SourceKind.BANK or left.kind is TxnKind.SETTLEMENT
    right_is_settlement = right.source is SourceKind.BANK or right.kind is TxnKind.SETTLEMENT

    if left_is_settlement == right_is_settlement:
        # Same side of the settlement boundary: the dates should simply agree.
        a = left.settled_on or left.occurred_on
        b = right.settled_on or right.occurred_on
        gap = abs((a - b).days)
        if gap == 0:
            return Level.EXACT
        if gap <= 1:
            return Level.STRONG
        if gap <= 7:
            return Level.WEAK
        return Level.DISAGREE

    payer, receiver = (right, left) if left_is_settlement else (left, right)
    landed = receiver.settled_on or receiver.occurred_on
    expected = calendar.settlement_date(payer.occurred_at, cycle_days=cycle_days)
    low, high = calendar.expected_window(payer.occurred_at, cycle_days=cycle_days, slack_days=1)

    if landed == expected:
        return Level.EXACT
    if low <= landed <= high:
        return Level.STRONG
    if abs((landed - expected).days) <= 7:
        return Level.WEAK
    return Level.DISAGREE


def _counterparty_level(left: CanonicalTxn, right: CanonicalTxn) -> Level:
    a, b = left.counterparty.strip(), right.counterparty.strip()
    if not a or not b:
        return Level.MISSING
    if a == b:
        return Level.EXACT
    if name_key(a) and name_key(a) == name_key(b):
        return Level.STRONG
    if counterparty_tokens(a) & counterparty_tokens(b):
        return Level.WEAK
    return Level.DISAGREE


def _narration_level(left: CanonicalTxn, right: CanonicalTxn) -> Level:
    a = (left.raw_narration or left.counterparty).strip()
    b = (right.raw_narration or right.counterparty).strip()
    if not a or not b:
        return Level.MISSING
    similarity = float(embed(a) @ embed(b))  # numpy scalar -> float
    if similarity >= 0.75:
        return Level.STRONG
    if similarity >= 0.40:
        return Level.WEAK
    return Level.DISAGREE


_COMPARATORS = (
    _reference_level,
    _amount_level,
    _date_level,
    _counterparty_level,
    _narration_level,
)


def compare(left: CanonicalTxn, right: CanonicalTxn) -> tuple[Level, ...]:
    """The comparison vector for one pair: one agreement level per field."""
    return tuple(comparator(left, right) for comparator in _COMPARATORS)


# --------------------------------------------------------------------------- #
# The model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class FieldWeight:
    """One field's contribution to one pair's score, in bits, with its reason."""

    field: str
    level: Level
    weight: float
    description: str

    def render(self) -> str:
        sign = "+" if self.weight >= 0 else ""
        return f"{self.field:14} {sign}{self.weight:6.2f} bits  {self.description}"


@dataclass(frozen=True, slots=True)
class PairScore:
    """A scored candidate pair, and the breakdown that explains it."""

    pair: Pair
    total: float
    weights: tuple[FieldWeight, ...]
    probability: float

    def render(self) -> str:
        lines = [f"match weight {self.total:+.2f} bits  (P(match) ~ {self.probability:.4f})"]
        for weight in sorted(self.weights, key=lambda w: -abs(w.weight)):
            lines.append("  " + weight.render())
        return "\n".join(lines)

    def as_confidence(self) -> Decimal:
        """Probability as a Decimal, since it feeds the policy engine."""
        return Decimal(str(round(self.probability, 6)))


@dataclass(slots=True)
class FellegiSunterModel:
    """EM-fitted m and u probabilities, plus the weights derived from them."""

    m: list[list[float]] = field(default_factory=list)
    u: list[list[float]] = field(default_factory=list)
    lambda_: float = 0.1
    iterations: int = 0
    converged: bool = False
    log_likelihood: float = 0.0

    # --- weights -----------------------------------------------------------
    def weight(self, field_index: int, level: Level) -> float:
        """log2(m/u) for one field at one level, clamped.

        MISSING is worth exactly zero: a field neither side carries is not evidence
        for or against, and letting EM assign it a weight would let absence of data
        masquerade as data.
        """
        if level is Level.MISSING:
            return 0.0
        m = self.m[field_index][int(level)]
        u = self.u[field_index][int(level)]
        if m <= 0 or u <= 0:
            return -WEIGHT_CLAMP if m <= 0 else WEIGHT_CLAMP
        return max(-WEIGHT_CLAMP, min(WEIGHT_CLAMP, math.log2(m / u)))

    def score(self, levels: Sequence[Level]) -> float:
        return float(sum(self.weight(i, level) for i, level in enumerate(levels)))

    def probability(self, levels: Sequence[Level]) -> float:
        """P(match | comparison vector) under the fitted model."""
        prior = math.log2(self.lambda_ / (1 - self.lambda_)) if 0 < self.lambda_ < 1 else 0.0
        odds = prior + self.score(levels)
        # 2**odds can overflow long before the probability stops being ~1.
        if odds > 60:
            return 1.0
        if odds < -60:
            return 0.0
        value: float = 2.0**odds
        return float(value / (1.0 + value))

    def explain(self, levels: Sequence[Level]) -> tuple[FieldWeight, ...]:
        return tuple(
            FieldWeight(
                field=FIELDS[i].name,
                level=level,
                weight=self.weight(i, level),
                description=FIELDS[i].describe.get(level, str(level)),
            )
            for i, level in enumerate(levels)
        )

    def score_pair(self, pair: Pair, levels: Sequence[Level]) -> PairScore:
        return PairScore(
            pair=pair,
            total=self.score(levels),
            weights=self.explain(levels),
            probability=self.probability(levels),
        )

    def weight_table(self) -> str:
        """Every field, every level, with its fitted m, u and weight.

        The single most useful artefact for convincing a finance team the model is
        not a black box: it is small enough to read in full.
        """
        lines = [f"{'field':14} {'level':10} {'m':>9} {'u':>9} {'weight':>9}"]
        lines.append("-" * 56)
        for i, spec in enumerate(FIELDS):
            for level in (Level.EXACT, Level.STRONG, Level.WEAK, Level.DISAGREE):
                if int(level) >= len(self.m[i]):
                    continue
                lines.append(
                    f"{spec.name:14} {level.name:10} {self.m[i][int(level)]:9.5f} "
                    f"{self.u[i][int(level)]:9.5f} {self.weight(i, level):+9.2f}"
                )
        lines.append(f"\nlambda (estimated share of candidate pairs that match) = {self.lambda_:.5f}")
        lines.append(f"EM: {self.iterations} iteration(s), converged={self.converged}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# EM
# --------------------------------------------------------------------------- #
def fit_em(
    vectors: Sequence[Sequence[Level]],
    *,
    max_iterations: int = 100,
    tolerance: float = 1e-7,
    seed_lambda: float = 0.10,
) -> FellegiSunterModel:
    """Estimate m, u and lambda by expectation-maximisation. No labels required.

    The initialisation matters more than the iteration count. Starting from m == u
    would be a saddle point that EM never leaves, so the two are seeded apart -
    optimistic about agreement under the match hypothesis, pessimistic under the
    non-match one. That is a genuine prior, and stating it is better than pretending
    the fit is assumption-free.
    """
    n_fields = len(FIELDS)
    n_levels = len(Level)
    if not vectors:
        return FellegiSunterModel(
            m=[[1.0 / n_levels] * n_levels for _ in range(n_fields)],
            u=[[1.0 / n_levels] * n_levels for _ in range(n_fields)],
            lambda_=seed_lambda,
        )

    # Seed: matches are assumed to agree, non-matches to disagree.
    m = [[0.05, 0.10, 0.25, 0.60, 0.0] for _ in range(n_fields)]
    u = [[0.70, 0.20, 0.07, 0.03, 0.0] for _ in range(n_fields)]
    lambda_ = seed_lambda

    previous = -math.inf
    model = FellegiSunterModel(m=m, u=u, lambda_=lambda_)
    converged = False
    iterations = 0

    for iterations in range(1, max_iterations + 1):
        # --- E step: responsibility of the match component for each pair -----
        responsibilities: list[float] = []
        log_likelihood = 0.0
        for levels in vectors:
            log_m = math.log(lambda_) if lambda_ > 0 else -60.0
            log_u = math.log(1 - lambda_) if lambda_ < 1 else -60.0
            for i, level in enumerate(levels):
                if level is Level.MISSING:
                    continue
                log_m += math.log(max(m[i][int(level)], 1e-12))
                log_u += math.log(max(u[i][int(level)], 1e-12))
            top = max(log_m, log_u)
            denominator = math.exp(log_m - top) + math.exp(log_u - top)
            responsibilities.append(math.exp(log_m - top) / denominator)
            log_likelihood += top + math.log(denominator)

        # --- M step: re-estimate from the responsibilities -------------------
        total_match = sum(responsibilities)
        total_non_match = len(responsibilities) - total_match
        lambda_ = min(max(total_match / len(responsibilities), 1e-6), 1 - 1e-6)

        new_m = [[SMOOTHING] * n_levels for _ in range(n_fields)]
        new_u = [[SMOOTHING] * n_levels for _ in range(n_fields)]
        for levels, responsibility in zip(vectors, responsibilities, strict=True):
            for i, level in enumerate(levels):
                if level is Level.MISSING:
                    continue
                new_m[i][int(level)] += responsibility
                new_u[i][int(level)] += 1.0 - responsibility

        for i in range(n_fields):
            m_total = sum(new_m[i][:4]) or 1.0
            u_total = sum(new_u[i][:4]) or 1.0
            m[i] = [value / m_total for value in new_m[i]]
            u[i] = [value / u_total for value in new_u[i]]
            m[i][int(Level.MISSING)] = 0.0
            u[i][int(Level.MISSING)] = 0.0

        model = FellegiSunterModel(
            m=[row[:] for row in m],
            u=[row[:] for row in u],
            lambda_=lambda_,
            iterations=iterations,
            log_likelihood=log_likelihood,
        )
        if abs(log_likelihood - previous) < tolerance * max(abs(previous), 1.0):
            converged = True
            break
        previous = log_likelihood
        _ = total_non_match

    return FellegiSunterModel(
        m=model.m,
        u=model.u,
        lambda_=model.lambda_,
        iterations=iterations,
        converged=converged,
        log_likelihood=model.log_likelihood,
    )


def score_candidates(
    rows: dict[str, CanonicalTxn], pairs: Sequence[Pair]
) -> tuple[FellegiSunterModel, list[PairScore]]:
    """Compare, fit, then score - in that order, and on the same pairs.

    EM is fitted on exactly the candidate set it will be used to score, which is what
    makes ``u`` meaningful: it is the distribution of agreement among pairs that
    *survived blocking*, not among all pairs in the universe.
    """
    ordered = sorted(pairs)
    vectors = [compare(rows[left], rows[right]) for left, right in ordered]
    model = fit_em(vectors)
    scores = [model.score_pair(pair, levels) for pair, levels in zip(ordered, vectors, strict=True)]
    return model, scores
