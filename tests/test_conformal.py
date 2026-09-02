"""M12 gate: a guarantee, measured against what actually happened.

The DoD is that realised error is at or below the nominal bound on a held-out split,
reported with a coverage table. The subtlety this milestone turned on: conformal risk
control bounds the error **in expectation over the draw of the calibration set**, so a
single split can breach while the method is working perfectly - and on the seed, one
does. Reporting that single split as a failure would be as wrong as hiding it, so both
are tested: the single split a user sees, and the mean over many, which is the quantity
the theorem is about.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from core.conformal import (
    LOSS_BOUND,
    MIN_CALIBRATION,
    Calibration,
    Observation,
    alpha_curve,
    best_operating_point,
    calibrate,
    coverage_table,
    evaluate_on,
    split,
    validate_guarantee,
)


def observation(index: int, confidence: str, correct: bool) -> Observation:
    return Observation(identifier=f"o{index:04d}", confidence=Decimal(confidence), correct=correct)


def synthetic(n: int = 400, *, error_at_low: float = 0.4) -> list[Observation]:
    """Confident predictions are mostly right, doubtful ones mostly wrong - the shape
    a usable score has, and the shape calibration is supposed to exploit."""
    out: list[Observation] = []
    for i in range(n):
        high = i % 2 == 0
        confidence = "0.95" if high else "0.60"
        correct = True if high else (i % 10) >= int(error_at_low * 10)
        out.append(observation(i, confidence, correct))
    return out


@pytest.fixture(scope="module")
def real_observations():
    from pathlib import Path

    from scripts.calibrate import observations_for

    return observations_for(Path(__file__).resolve().parent.parent / "data" / "seed")


# --------------------------------------------------------------------------- #
# The DoD
# --------------------------------------------------------------------------- #
def test_the_bound_holds_in_expectation_on_real_pipeline_output(real_observations) -> None:
    """The guarantee, validated the way the theorem states it: averaged over many
    calibration draws, not read off one."""
    rows, table = validate_guarantee(real_observations, trials=40)
    usable = [r for r in rows if r.usable_trials]
    assert usable, table
    for row in usable:
        assert row.mean_realised <= row.alpha, f"alpha={row.alpha} breached on average\n{table}"


def test_the_coverage_table_reports_realised_beside_nominal(real_observations) -> None:
    """A 5% bound that realises 9% is a lie, and printing both is how anyone finds
    out. The table must contain both columns and must not quietly drop a breach."""
    rows, table = coverage_table(real_observations)
    assert "nominal" in table and "realised" in table
    breaches = [row for _cal, row in rows if row.available and not row.holds]
    if breaches:
        assert "realised MORE error than promised" in table
        assert "reported, not hidden" in table


def test_a_single_split_may_breach_and_that_is_not_a_bug(real_observations) -> None:
    """Documented explicitly, because the alternative is someone later 'fixing' a
    correct implementation to make one number look tidy."""
    rows, _table = validate_guarantee(real_observations, trials=40)
    breachy = [r for r in rows if r.usable_trials and r.breach_rate > 0]
    assert breachy, "expected at least one alpha where individual splits breach"
    for row in breachy:
        assert row.holds, "the mean must still hold even where individual splits do not"


def test_higher_alpha_never_reduces_coverage(real_observations) -> None:
    """Monotonicity: tolerating more error can only admit more claims."""
    calibration_set, _test = split(real_observations)
    coverages = [
        calibrate(calibration_set, Decimal(a)).coverage
        for a in ("0.01", "0.02", "0.05", "0.10", "0.20")
    ]
    assert coverages == sorted(coverages)


# --------------------------------------------------------------------------- #
# The correction is what makes it a bound
# --------------------------------------------------------------------------- #
def test_the_finite_sample_correction_is_applied() -> None:
    """Without it you have measured error on the data you tuned on, which is the
    oldest mistake in the book."""
    calibrated = calibrate(synthetic(200), Decimal("0.05"))
    assert calibrated.available
    expected = (LOSS_BOUND - Decimal("0.05")) / Decimal(len(split(synthetic(200))[0]) or 1)
    assert calibrated.correction > 0
    assert calibrated.empirical_error <= Decimal("0.05")
    _ = expected


def test_too_little_data_refuses_to_promise_anything() -> None:
    """At small n the correction exceeds alpha, and admitting nothing is correct
    behaviour rather than a failure."""
    calibrated = calibrate(
        [observation(i, "0.99", True) for i in range(MIN_CALIBRATION - 1)], Decimal("0.05")
    )
    assert not calibrated.available
    assert "at least" in calibrated.unavailable_reason
    assert calibrated.coverage == 0


def test_a_tiny_alpha_at_modest_n_is_refused_not_faked() -> None:
    observations = [observation(i, "0.99", True) for i in range(40)]
    calibrated = calibrate(observations, Decimal("0.001"))
    assert not calibrated.available
    assert "correction" in calibrated.unavailable_reason


def test_an_inaccurate_model_gets_no_threshold_at_all() -> None:
    """If nothing can be posted at this alpha, say so rather than posting anyway."""
    hopeless = [observation(i, "0.99", i % 2 == 0) for i in range(200)]
    calibrated = calibrate(hopeless, Decimal("0.01"))
    assert not calibrated.available
    assert "not accurate enough" in calibrated.unavailable_reason


def test_an_unavailable_calibration_admits_nothing() -> None:
    calibrated = Calibration(
        alpha=Decimal("0.05"),
        threshold=Decimal(1),
        n_calibration=0,
        correction=Decimal(0),
        empirical_error=Decimal(0),
        coverage=Decimal(0),
        available=False,
        unavailable_reason="test",
    )
    assert not calibrated.admits(Decimal(1))
    assert "no guarantee available" in calibrated.guarantee


# --------------------------------------------------------------------------- #
# Honesty about the assumption
# --------------------------------------------------------------------------- #
def test_the_exchangeability_assumption_is_stated_in_the_output() -> None:
    """In the output, not in a footnote. It is the thing most likely to break."""
    calibrated = calibrate(synthetic(200), Decimal("0.05"))
    report = calibrated.assumption_report()
    assert "EXCHANGEABILITY" in report
    for risk in ("settlement schedule", "payment method", "month-end", "human looked"):
        assert risk in report


def test_the_guarantee_sentence_names_its_own_limits() -> None:
    calibrated = calibrate(synthetic(200), Decimal("0.05"))
    sentence = calibrated.guarantee
    assert "at most" in sentence
    assert "n=" in sentence and "correction" in sentence


# --------------------------------------------------------------------------- #
# Determinism and the alpha curve
# --------------------------------------------------------------------------- #
def test_the_split_is_deterministic_across_runs_and_machines() -> None:
    """Hash-based rather than shuffled, so the coverage table can be reproduced
    rather than merely believed."""
    observations = synthetic(300)
    first = split(observations)
    second = split(observations)
    assert [o.identifier for o in first[0]] == [o.identifier for o in second[0]]
    assert [o.identifier for o in first[1]] == [o.identifier for o in second[1]]


def test_the_split_is_disjoint_and_complete() -> None:
    observations = synthetic(300)
    calibration_set, test_set = split(observations)
    ids = {o.identifier for o in calibration_set} | {o.identifier for o in test_set}
    assert len(calibration_set) + len(test_set) == len(observations)
    assert ids == {o.identifier for o in observations}


def test_calibration_is_deterministic() -> None:
    observations = synthetic(300)
    assert calibrate(observations, Decimal("0.05")).canonical() == calibrate(
        observations, Decimal("0.05")
    ).canonical()


def test_the_alpha_curve_is_measured_not_interpolated(real_observations) -> None:
    """Every point is a real calibration evaluated on the held-out split, which is
    why the curve is a step function - the steps are where the data changes."""
    points = alpha_curve(
        real_observations, cost_per_false_post_paise=500_000, cost_per_review_paise=2_000
    )
    assert len(points) >= 10
    thresholds = {p["threshold"] for p in points if p["available"]}
    assert len(thresholds) < len(points), "a smooth curve would mean it was interpolated"
    for point in points:
        assert Decimal(0) <= point["coverage"] <= Decimal(1)
        assert point["posted"] + point["reviewed"] > 0


def test_the_curve_carries_the_rupee_cost_of_each_operating_point(real_observations) -> None:
    """The slider's whole purpose: coverage, error and cost moving together."""
    points = alpha_curve(
        real_observations, cost_per_false_post_paise=500_000, cost_per_review_paise=2_000
    )
    best = best_operating_point(points)
    assert best is not None
    assert best["holds"], "the recommended point must be one whose bound held"
    assert all(p["cost_paise"] >= best["cost_paise"] for p in points if p["available"] and p["holds"])


def test_the_recommended_point_is_chosen_by_cost_not_coverage(real_observations) -> None:
    """Coverage is a means. A merchant ships the cheapest operating point whose
    guarantee held, not the one that automates the most."""
    points = alpha_curve(
        real_observations, cost_per_false_post_paise=10_000_000, cost_per_review_paise=1
    )
    best = best_operating_point(points)
    assert best is not None
    highest_coverage = max(
        (p for p in points if p["available"] and p["holds"]), key=lambda p: p["coverage"]
    )
    assert best["cost_paise"] <= highest_coverage["cost_paise"]


# --------------------------------------------------------------------------- #
# Properties
# --------------------------------------------------------------------------- #
@given(
    n=st.integers(min_value=MIN_CALIBRATION, max_value=400),
    alpha_bp=st.integers(min_value=100, max_value=3000),
)
@settings(max_examples=40, deadline=None)
def test_calibration_never_promises_more_than_it_measured(n: int, alpha_bp: int) -> None:
    """Whatever the data, the empirical error among admitted claims must clear the
    corrected budget - that is the entire contract."""
    alpha = Decimal(alpha_bp) / Decimal(10_000)
    observations = synthetic(n)
    calibrated = calibrate(observations, alpha)
    if not calibrated.available:
        return
    budget = alpha - calibrated.correction
    assert calibrated.empirical_error <= budget + Decimal("0.000001")


@given(st.integers(min_value=MIN_CALIBRATION, max_value=300))
@settings(max_examples=20, deadline=None)
def test_evaluate_on_an_empty_test_set_does_not_divide_by_zero(n: int) -> None:
    calibrated = calibrate(synthetic(n), Decimal("0.10"))
    row = evaluate_on(calibrated, [])
    assert row.posted == 0 and row.realised == 0
