"""A guarantee instead of a hunch.

Every reconciliation tool has a confidence threshold, and almost every one of them
picked it the same way: someone tried 0.7, the results looked reasonable, and 0.7
shipped. That number means nothing. It is not a probability of anything, it does not
transfer to next month's data, and when it is wrong nobody can say by how much.

Split conformal calibration replaces it with a statement that is *checkable*:

    of the matches Triveni posts without a human, at most alpha will be wrong -
    and that holds without assuming the score is calibrated, that errors are
    Gaussian, or that the model is any good.

**How.** Hold out a labelled calibration slice. For each candidate compute a
nonconformity score - here, one minus the model's confidence, so a confident match
scores near zero. Sweep the threshold and, for each one, measure the empirical error
among the matches it would auto-post. Then apply the finite-sample correction from
conformal risk control (Angelopoulos, Bates et al.) and take the most permissive
threshold that survives it:

    q_hat = inf { t : R_hat(t) <= alpha - (B - alpha) / n }

with ``B = 1`` the loss bound and ``n`` the calibration size. The correction is what
makes this a bound rather than an in-sample observation: without it you have measured
the error on the data you tuned on, which is the oldest mistake there is. It also
explains why a small calibration set buys a weak guarantee - at n = 40 and alpha = 0.05
the correction is larger than alpha itself, so the method correctly refuses to promise
anything and abstains on everything.

**The assumption, stated plainly.** Conformal prediction requires *exchangeability*
between calibration and test data. That is weaker than i.i.d. but it is not nothing,
and in reconciliation it is the thing most likely to break:

* a gateway changes its settlement schedule, and the timing distribution shifts;
* a merchant adds a payment method whose fee structure the model has never seen;
* month-end volume is not exchangeable with mid-month volume;
* the calibration set comes from *labelled* data, and rows get labelled because a
  human looked at them - which is not a random sample of anything.

So the guarantee is conditional on an assumption that reconciliation data routinely
violates. :meth:`Calibration.assumption_report` says so in the output rather than in a
footnote, and :func:`coverage_table` reports **realised error on a held-out split
beside the nominal bound**, because a 5% bound that realises 9% is a lie and printing
both is the only way anyone finds out.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final

#: Loss bound. The loss here is 0/1 - a posted match is either right or wrong.
LOSS_BOUND: Final = Decimal(1)

#: Below this many calibration points the correction swamps any useful alpha, and the
#: honest response is to say the guarantee is unavailable rather than to issue a weak
#: one dressed as a strong one.
MIN_CALIBRATION: Final = 30

NEWLINE: Final = "\n"


def _q(value: Decimal | float, places: str = "0.000001") -> Decimal:
    return Decimal(str(value)).quantize(Decimal(places), rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True, slots=True)
class Observation:
    """One labelled candidate: what we scored it, and whether it was right."""

    identifier: str
    confidence: Decimal
    correct: bool
    amount_paise: int = 0

    @property
    def nonconformity(self) -> Decimal:
        """Low for confident predictions, high for doubtful ones."""
        return Decimal(1) - self.confidence


@dataclass(frozen=True, slots=True)
class Calibration:
    """A threshold with a bound attached, and the honesty to describe its limits."""

    alpha: Decimal
    threshold: Decimal
    """Minimum confidence to auto-post. Above it we act; below it a human decides."""

    n_calibration: int
    correction: Decimal
    """The finite-sample penalty. Large relative to alpha means a weak guarantee."""

    empirical_error: Decimal
    """Error among auto-posted matches, measured on the calibration slice."""

    coverage: Decimal
    """Share of candidates the threshold admits."""

    available: bool = True
    unavailable_reason: str = ""

    @property
    def guarantee(self) -> str:
        if not self.available:
            return f"no guarantee available: {self.unavailable_reason}"
        return (
            f"of the matches auto-posted at confidence >= {self.threshold}, at most "
            f"{self.alpha:.1%} are expected to be wrong "
            f"(split conformal, n={self.n_calibration}, correction {self.correction:.4f})"
        )

    def admits(self, confidence: Decimal) -> bool:
        return self.available and confidence >= self.threshold

    def assumption_report(self) -> str:
        """What has to be true for the bound to hold, and where it tends not to be."""
        return (
            "This bound assumes EXCHANGEABILITY between the calibration slice and the "
            "rows it is applied to. That is weaker than i.i.d., but reconciliation "
            "data violates it routinely: a gateway changing its settlement schedule "
            "shifts the timing distribution; a new payment method has a fee structure "
            "the model has never seen; month-end volume is not exchangeable with "
            "mid-month; and calibration labels exist because a human looked at those "
            "rows, which is not a random sample. Treat the number as a well-founded "
            "operating point that must be re-calibrated when the data moves, not as a "
            "standing promise."
        )

    def canonical(self) -> dict[str, object]:
        return {
            "alpha": self.alpha,
            "threshold": self.threshold,
            "n_calibration": self.n_calibration,
            "correction": self.correction,
            "empirical_error": self.empirical_error,
            "coverage": self.coverage,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
        }


def calibrate(
    observations: Sequence[Observation],
    alpha: Decimal = Decimal("0.05"),
    *,
    min_calibration: int = MIN_CALIBRATION,
) -> Calibration:
    """Find the most permissive threshold whose risk bound survives the correction.

    Sweeps every distinct confidence in the calibration set as a candidate threshold,
    keeps those whose empirical error clears ``alpha - (B - alpha) / n``, and returns
    the one admitting the most candidates. Ties break toward the *higher* threshold,
    because when two operating points admit the same set the more conservative one
    generalises better.
    """
    if len(observations) < min_calibration:
        return Calibration(
            alpha=alpha,
            threshold=Decimal(1),
            n_calibration=len(observations),
            correction=Decimal(0),
            empirical_error=Decimal(0),
            coverage=Decimal(0),
            available=False,
            unavailable_reason=(
                f"only {len(observations)} labelled calibration point(s); at least "
                f"{min_calibration} are needed before a bound means anything"
            ),
        )

    n = len(observations)
    # Conformal risk control's finite-sample correction. At small n this exceeds
    # alpha, and the method then admits nothing - which is the correct behaviour, not
    # a bug: there is genuinely not enough evidence to promise anything.
    correction = (LOSS_BOUND - alpha) / Decimal(n)
    budget = alpha - correction

    if budget <= 0:
        return Calibration(
            alpha=alpha,
            threshold=Decimal(1),
            n_calibration=n,
            correction=_q(correction),
            empirical_error=Decimal(0),
            coverage=Decimal(0),
            available=False,
            unavailable_reason=(
                f"the finite-sample correction ({correction:.4f}) exceeds alpha "
                f"({alpha}); {n} calibration points cannot support this bound"
            ),
        )

    candidates = sorted({o.confidence for o in observations}, reverse=True)
    best: tuple[Decimal, Decimal, Decimal] | None = None  # (coverage, threshold, error)

    for threshold in candidates:
        admitted = [o for o in observations if o.confidence >= threshold]
        if not admitted:
            continue
        wrong = sum(1 for o in admitted if not o.correct)
        error = Decimal(wrong) / Decimal(len(admitted))
        if error > budget:
            continue
        coverage = Decimal(len(admitted)) / Decimal(n)
        if best is None or coverage > best[0]:
            best = (coverage, threshold, error)

    if best is None:
        return Calibration(
            alpha=alpha,
            threshold=Decimal(1),
            n_calibration=n,
            correction=_q(correction),
            empirical_error=Decimal(0),
            coverage=Decimal(0),
            available=False,
            unavailable_reason=(
                f"no threshold achieves an error at or below {budget:.4f}; the model "
                f"is not accurate enough to auto-post anything at alpha={alpha}"
            ),
        )

    coverage, threshold, error = best
    return Calibration(
        alpha=alpha,
        threshold=threshold,
        n_calibration=n,
        correction=_q(correction),
        empirical_error=_q(error),
        coverage=_q(coverage),
    )


# --------------------------------------------------------------------------- #
# Measuring the guarantee
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class CoverageRow:
    """One row of the honesty table: what was promised, what actually happened."""

    alpha: Decimal
    threshold: Decimal
    nominal: Decimal
    realised: Decimal
    """Error among auto-posted matches on the HELD-OUT split. The number that matters."""

    coverage: Decimal
    posted: int
    wrong: int
    reviewed: int
    holds: bool
    available: bool = True

    def render(self) -> str:
        if not self.available:
            return f"  {self.alpha:>6.1%}  {'no guarantee available':>48}"
        mark = "ok " if self.holds else "OVER"
        return (
            f"  {self.alpha:>6.1%}  {self.threshold:>9}  {self.nominal:>8.2%}  "
            f"{self.realised:>8.2%}  {self.coverage:>8.1%}  {self.posted:>6}  "
            f"{self.wrong:>5}  {self.reviewed:>8}  {mark}"
        )


def split(
    observations: Sequence[Observation], *, ratio: Decimal = Decimal("0.5"), seed: int = 20260101
) -> tuple[list[Observation], list[Observation]]:
    """Deterministic calibration/test split.

    Hash-based rather than shuffled, so the split depends only on the identifiers and
    is identical on every machine and every run - which is what lets the coverage
    table be reproduced rather than merely believed.
    """
    calibration: list[Observation] = []
    test: list[Observation] = []
    cut = int(float(ratio) * (2**32))
    for observation in sorted(observations, key=lambda o: o.identifier):
        digest = int.from_bytes(
            __import__("hashlib")
            .blake2b(f"{seed}:{observation.identifier}".encode(), digest_size=4)
            .digest(),
            "big",
        )
        (calibration if digest < cut else test).append(observation)
    return calibration, test


def evaluate_on(calibration: Calibration, test: Sequence[Observation]) -> CoverageRow:
    """Apply a calibrated threshold to held-out data and report what really happened.

    This is the whole point of the milestone. A bound measured on the data used to
    choose it is not a bound.
    """
    if not calibration.available:
        return CoverageRow(
            alpha=calibration.alpha,
            threshold=calibration.threshold,
            nominal=calibration.alpha,
            realised=Decimal(0),
            coverage=Decimal(0),
            posted=0,
            wrong=0,
            reviewed=len(test),
            holds=True,
            available=False,
        )

    posted = [o for o in test if calibration.admits(o.confidence)]
    wrong = sum(1 for o in posted if not o.correct)
    realised = Decimal(wrong) / Decimal(len(posted)) if posted else Decimal(0)
    return CoverageRow(
        alpha=calibration.alpha,
        threshold=calibration.threshold,
        nominal=calibration.alpha,
        realised=_q(realised),
        coverage=_q(Decimal(len(posted)) / Decimal(len(test))) if test else Decimal(0),
        posted=len(posted),
        wrong=wrong,
        reviewed=len(test) - len(posted),
        holds=realised <= calibration.alpha,
    )


def coverage_table(
    observations: Sequence[Observation],
    alphas: Sequence[Decimal] | None = None,
    *,
    min_calibration: int = MIN_CALIBRATION,
) -> tuple[list[tuple[Calibration, CoverageRow]], str]:
    """Calibrate at each alpha and measure each on the same held-out split.

    Returns the rows plus a rendered table. This is the data behind the UI's alpha
    slider: every point on it is a real operating point that was actually measured,
    not an interpolation.
    """
    alphas = alphas or [
        Decimal("0.01"),
        Decimal("0.02"),
        Decimal("0.05"),
        Decimal("0.10"),
        Decimal("0.20"),
    ]
    calibration_set, test_set = split(observations)

    rows: list[tuple[Calibration, CoverageRow]] = []
    for alpha in alphas:
        calibrated = calibrate(calibration_set, alpha, min_calibration=min_calibration)
        rows.append((calibrated, evaluate_on(calibrated, test_set)))

    lines = [
        f"calibration n={len(calibration_set)}   held-out n={len(test_set)}",
        "",
        f"  {'alpha':>6}  {'threshold':>9}  {'nominal':>8}  {'realised':>8}  "
        f"{'coverage':>8}  {'posted':>6}  {'wrong':>5}  {'reviewed':>8}",
        "  " + "-" * 74,
    ]
    lines.extend(row.render() for _cal, row in rows)
    breaches = [row for _cal, row in rows if row.available and not row.holds]
    lines.append("")
    if breaches:
        lines.append(
            f"  {len(breaches)} alpha(s) realised MORE error than promised on the "
            f"held-out split - reported, not hidden."
        )
    else:
        lines.append("  every bound held on the held-out split.")
    return rows, "\n".join(lines)


def alpha_curve(
    observations: Sequence[Observation],
    *,
    cost_per_false_post_paise: int,
    cost_per_review_paise: int,
    steps: int = 21,
) -> list[dict[str, object]]:
    """The curve the UI slider travels along: alpha against coverage, error and rupees.

    Computed rather than interpolated - each point is a real calibration measured on
    the held-out split, which is why dragging the slider shows a step function rather
    than a smooth line. The steps are where the data actually changes.
    """
    calibration_set, test_set = split(observations)

    # A geometric grid, not a linear one. The interesting region is the low end: at
    # alpha=1% the threshold climbs to admit only the confident claims, and at 5% it
    # has already flattened out. A linear grid from 1/(2*steps) upward starts at 2.4%
    # and misses the entire trade - every point on it reported the same threshold, the
    # same coverage and the same cost, which looked like a working curve and was not.
    grid = [
        _q(Decimal("0.002") * (Decimal("1.35") ** index), "0.0001") for index in range(steps)
    ]

    points: list[dict[str, object]] = []
    for alpha in grid:
        if alpha > Decimal("0.5"):
            break
        calibrated = calibrate(calibration_set, alpha)
        row = evaluate_on(calibrated, test_set)
        cost = row.wrong * cost_per_false_post_paise + row.reviewed * cost_per_review_paise
        points.append(
            {
                "alpha": alpha,
                "available": row.available,
                "threshold": calibrated.threshold,
                "coverage": row.coverage,
                "realised_error": row.realised,
                "posted": row.posted,
                "wrong": row.wrong,
                "reviewed": row.reviewed,
                "cost_paise": cost,
                "holds": row.holds,
            }
        )
    return points


def best_operating_point(points: Sequence[dict[str, object]]) -> dict[str, object] | None:
    """The alpha a merchant would actually ship: lowest rupee cost among bounds that
    held. Cost, not coverage - coverage is a means."""
    usable = [p for p in points if p["available"] and p["holds"]]
    if not usable:
        return None
    return min(usable, key=lambda p: (p["cost_paise"], p["alpha"]))


# --------------------------------------------------------------------------- #
# Validating the guarantee properly
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ValidationRow:
    """What the bound promises versus what it delivers, averaged over many splits."""

    alpha: Decimal
    trials: int
    mean_realised: Decimal
    worst_realised: Decimal
    breach_rate: Decimal
    """Share of splits where the realised error exceeded alpha."""

    mean_coverage: Decimal
    usable_trials: int

    @property
    def holds(self) -> bool:
        """The guarantee is about the *expectation*, so that is what is checked."""
        return self.mean_realised <= self.alpha

    def render(self) -> str:
        if not self.usable_trials:
            return f"  {self.alpha:>6.1%}  {'no usable calibration in any split':>52}"
        mark = "ok " if self.holds else "OVER"
        return (
            f"  {self.alpha:>6.1%}  {self.mean_realised:>9.2%}  {self.worst_realised:>9.2%}  "
            f"{self.breach_rate:>9.1%}  {self.mean_coverage:>9.1%}  {self.usable_trials:>7}  {mark}"
        )


def validate_guarantee(
    observations: Sequence[Observation],
    alphas: Sequence[Decimal] | None = None,
    *,
    trials: int = 40,
    min_calibration: int = MIN_CALIBRATION,
) -> tuple[list[ValidationRow], str]:
    """Re-split many times and check the bound holds *in expectation*.

    Conformal risk control promises ``E[R] <= alpha`` where the expectation is over
    the draw of the calibration set. A single split can therefore breach the bound
    while the method is working perfectly - and on the seed, one does. Reporting that
    single split as a failure would be as wrong as hiding it.

    So both are reported: the one split a user actually sees, and the mean over many,
    which is the quantity the theorem is about. The breach *rate* is reported too,
    because "the mean holds but 40% of individual runs breach" is a materially
    different product than "the mean holds and 2% breach".
    """
    alphas = alphas or [
        Decimal("0.01"),
        Decimal("0.02"),
        Decimal("0.05"),
        Decimal("0.10"),
        Decimal("0.20"),
    ]
    rows: list[ValidationRow] = []
    for alpha in alphas:
        realised: list[Decimal] = []
        coverages: list[Decimal] = []
        breaches = 0
        for trial in range(trials):
            calibration_set, test_set = split(observations, seed=20260101 + trial * 7919)
            calibrated = calibrate(calibration_set, alpha, min_calibration=min_calibration)
            if not calibrated.available or not test_set:
                continue
            row = evaluate_on(calibrated, test_set)
            realised.append(row.realised)
            coverages.append(row.coverage)
            if not row.holds:
                breaches += 1

        if not realised:
            rows.append(
                ValidationRow(alpha, trials, Decimal(0), Decimal(0), Decimal(0), Decimal(0), 0)
            )
            continue

        rows.append(
            ValidationRow(
                alpha=alpha,
                trials=trials,
                mean_realised=_q(sum(realised) / Decimal(len(realised))),
                worst_realised=max(realised),
                breach_rate=_q(Decimal(breaches) / Decimal(len(realised))),
                mean_coverage=_q(sum(coverages) / Decimal(len(coverages))),
                usable_trials=len(realised),
            )
        )

    lines = [
        f"guarantee validated over {trials} random calibration/test splits",
        "",
        f"  {'alpha':>6}  {'mean err':>9}  {'worst':>9}  {'breach %':>9}  "
        f"{'coverage':>9}  {'splits':>7}",
        "  " + "-" * 62,
    ]
    lines.extend(row.render() for row in rows)
    failed = [r for r in rows if r.usable_trials and not r.holds]
    lines.append("")
    lines.append(
        "  the bound is on the EXPECTATION over calibration draws, so the mean "
        "column is the one it promises; individual splits may and do exceed it."
    )
    if failed:
        lines.append(
            f"  {len(failed)} alpha(s) breach the bound ON AVERAGE - a real failure, "
            f"reported."
        )
    else:
        lines.append("  every bound holds in expectation.")
    return rows, NEWLINE.join(lines)
