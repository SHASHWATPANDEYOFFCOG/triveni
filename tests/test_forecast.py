"""M14 gate: the forecast ladder, conformal intervals, and the liquidity alert.

The DoD is a rolling-origin backtest table with MASE and pinball loss, and empirical
coverage published beside the nominal level. The tests below also pin the two errors
that produced a band which looked fine and was not: calibrating on data the model had
trained on, and sizing a 14-day band from one-step residuals.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from core.clock import INDIAN_BANK_CALENDAR
from core.money import Money
from forecast.alerts import (
    Commitment,
    CommitmentKind,
    Severity,
    check,
    cumulative_by,
    default_commitments,
)
from forecast.backtest import (
    Interval,
    conformal_width,
    conformalise,
    measure_coverage,
)
from forecast.features import (
    DailySeries,
    build_matrix,
    calendar_features,
    future_features,
    is_gst_window,
    is_salary_window,
)
from forecast.models import mase, pinball
from forecast.service import MIN_HISTORY_DAYS, build, forecast_cash, series_from_reconciliation

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "data" / "generated" / "history"


@pytest.fixture(scope="module")
def series():
    if not HISTORY.exists():
        pytest.skip("run `python -m data.gen --spec history` first")
    return series_from_reconciliation(HISTORY)


@pytest.fixture(scope="module")
def forecast(series):
    if len(series) < MIN_HISTORY_DAYS:
        pytest.skip("not enough history")
    return build(HISTORY)


# --------------------------------------------------------------------------- #
# The DoD: the backtest table
# --------------------------------------------------------------------------- #
def test_the_backtest_is_rolling_origin_not_a_single_split(forecast) -> None:
    """One split measures how a model did on one fortnight. Rolling origin measures
    how it behaves when you use it."""
    assert forecast.backtest.folds >= 4
    assert forecast.backtest.horizon == 14


def test_every_rung_of_the_ladder_is_scored_on_the_same_folds(forecast) -> None:
    names = {score.name for score in forecast.backtest.scores}
    assert names == {"seasonal_naive", "seasonal_drift", "theta", "ets", "boosted"}
    folds = {score.folds for score in forecast.backtest.scores}
    assert len(folds) == 1, "models were scored on different folds"


def test_mase_and_pinball_are_reported_and_mape_is_not(forecast) -> None:
    """MAPE is undefined on a zero-cash day, and this series is full of structural
    zeros - every Sunday and every 2nd and 4th Saturday."""
    table = forecast.backtest.render()
    assert "MASE" in table and "pinball" in table
    assert "MAPE" not in table.upper()
    for score in forecast.backtest.scores:
        assert score.mase > 0
        assert score.pinball_50 > 0


def test_beating_naive_is_measured_against_naive_not_against_one(forecast) -> None:
    """MASE scales by the *in-sample* naive error, so an out-of-sample naive forecast
    does not score 1.0 - here it scores ~0.84. Comparing everything to 1.0 labelled
    seasonal_drift (0.93) as beating a baseline it is materially worse than."""
    scores = {s.name: s for s in forecast.backtest.scores}
    naive = scores["seasonal_naive"]
    drift = scores["seasonal_drift"]
    assert naive.mase < Decimal(1), "the naive baseline should not score exactly 1.0"
    if drift.mase > naive.mase:
        assert not drift.beats_naive
        assert "LOSES to naive" in drift.render()


def test_the_winner_is_whichever_rung_actually_won(forecast) -> None:
    """A boosted model that cannot beat 'last Tuesday' has not earned its place, and
    the forecast must use the baseline in that case rather than the fanciest model."""
    best = forecast.backtest.best
    assert forecast.model == best.name
    assert all(best.mase <= s.mase for s in forecast.backtest.scores)


def test_a_baseline_win_would_be_published_as_such() -> None:
    """The rendering path for the honest-loss case, exercised so it cannot rot."""
    from forecast.backtest import BacktestResult, ModelScore

    naive = ModelScore("seasonal_naive", "d", Decimal("0.5"), Decimal(1), Decimal(1), Decimal(1), 3)
    boosted = ModelScore("boosted", "d", Decimal("0.9"), Decimal(2), Decimal(2), Decimal(2), 3)
    result = BacktestResult(
        (naive, boosted), 14, 3, dt.date(2026, 1, 1), dt.date(2026, 6, 1)
    )
    assert result.best.name == "seasonal_naive"
    assert "baseline wins" in result.render()
    assert "has not earned its place" in result.render()


# --------------------------------------------------------------------------- #
# The DoD: measured coverage
# --------------------------------------------------------------------------- #
def test_realised_coverage_is_published_beside_nominal(forecast) -> None:
    """A 90% band that contains the truth 71% of the time is a lie, and printing both
    is the only way anyone finds out."""
    rendered = forecast.coverage.render()
    assert "nominal" in rendered and "realised" in rendered
    assert forecast.coverage.n > 50


def test_the_band_actually_covers(forecast) -> None:
    """The whole point of conformalising.

    Asserted against nominal *within sampling error*, not strictly above it. At 84
    held-out days the binomial standard error on a 90% rate is about 3.3 points, so a
    strict test would fail roughly half the time on a band that covers perfectly - and
    a test that fails half the time gets deleted, which is worse than one that is
    honest about its own resolution. `holds` and `within_noise` are both reported, so
    a genuine regression beyond one standard error still fails here.
    """
    coverage = forecast.coverage
    assert coverage.within_noise, coverage.render()
    assert coverage.realised > Decimal("0.80"), coverage.render()


def test_coverage_reports_its_own_sampling_error(forecast) -> None:
    """'88.1% against a nominal 90%' means something very different at n=84 than at
    n=8,400, and reporting only the point estimate hides which one you have."""
    coverage = forecast.coverage
    assert coverage.standard_error > 0
    assert "SE" in coverage.render() or coverage.holds


def test_under_coverage_is_reported_not_hidden() -> None:
    from forecast.backtest import CoverageResult

    under = CoverageResult(Decimal("0.90"), Decimal("0.714"), 100, 5_000)
    assert not under.holds
    assert not under.within_noise, "a 19-point shortfall is not sampling noise"
    assert "UNDER" in under.render() and "beyond 1 SE" in under.render()
    assert under.gap < 0


def test_a_small_shortfall_is_distinguished_from_a_real_one() -> None:
    from forecast.backtest import CoverageResult

    noise = CoverageResult(Decimal("0.90"), Decimal("0.881"), 84, 5_000)
    real = CoverageResult(Decimal("0.90"), Decimal("0.700"), 84, 5_000)
    assert noise.within_noise and "inside 1 SE" in noise.render()
    assert not real.within_noise and "beyond 1 SE" in real.render()


def test_intervals_are_calibrated_per_horizon_step() -> None:
    """Forecast error grows with horizon. Sizing day 14's band from one-step residuals
    under-covers at the far end - it realised 77.4% against a 90% nominal."""
    dates = [dt.date(2026, 3, 1) + dt.timedelta(days=i) for i in range(3)]
    per_step = {1: [10.0] * 40, 2: [100.0] * 40, 3: [1000.0] * 40}
    intervals = conformalise([500.0, 500.0, 500.0], per_step, dates, Decimal("0.10"))
    widths = [i.upper - i.lower for i in intervals]
    assert widths[0] < widths[1] < widths[2], "later steps must get wider bands"


def test_a_flat_residual_sequence_still_works() -> None:
    dates = [dt.date(2026, 3, 1) + dt.timedelta(days=i) for i in range(3)]
    intervals = conformalise([100.0] * 3, [5.0] * 40, dates, Decimal("0.10"))
    assert all(i.upper > i.lower for i in intervals)


def test_the_lower_bound_is_floored_at_zero() -> None:
    """Cash cannot be negative, and an alert that fires on a band dipping to minus
    four lakh is an alert nobody trusts twice."""
    dates = [dt.date(2026, 3, 1)]
    intervals = conformalise([100.0], [10_000.0] * 40, dates, Decimal("0.10"))
    assert intervals[0].lower == 0


def test_the_conformal_width_carries_the_finite_sample_correction() -> None:
    residuals = [float(i) for i in range(100)]
    plain = float(np.quantile(np.abs(residuals), 0.9))
    corrected = conformal_width(residuals, Decimal("0.10"))
    assert corrected >= plain


def test_too_few_residuals_falls_back_to_the_worst_miss() -> None:
    """'I cannot be more precise than my worst miss' is the correct answer at small n."""
    assert conformal_width([3.0, -7.0, 5.0], Decimal("0.10")) == 7.0


def test_coverage_measurement_counts_containment() -> None:
    intervals = [Interval(dt.date(2026, 3, 1), 100, 50, 150), Interval(dt.date(2026, 3, 2), 100, 50, 150)]
    assert measure_coverage(intervals, [120, 900], Decimal("0.10")).realised == Decimal("0.5")


# --------------------------------------------------------------------------- #
# Calendar features
# --------------------------------------------------------------------------- #
def test_the_series_includes_closed_days_as_zeros(series) -> None:
    """A series that omits closed days teaches a model that consecutive rows are
    consecutive business days, and a three-day weekend looks like a one-day gap."""
    assert len(series) > 100
    assert any(value == 0 for value in series.values)
    for earlier, later in zip(series.dates, series.dates[1:], strict=False):
        assert (later - earlier).days == 1


def test_a_non_contiguous_series_is_refused() -> None:
    with pytest.raises(ValueError, match="contiguous"):
        DailySeries((dt.date(2026, 3, 1), dt.date(2026, 3, 5)), (1, 2))


def test_calendar_features_know_the_bank_is_shut() -> None:
    """The 2nd Saturday of March 2026 is the 14th."""
    open_day = calendar_features(dt.date(2026, 3, 10), INDIAN_BANK_CALENDAR)
    shut_day = calendar_features(dt.date(2026, 3, 14), INDIAN_BANK_CALENDAR)
    assert open_day[0] == 1.0
    assert shut_day[0] == 0.0


def test_salary_and_gst_windows_are_recognised() -> None:
    assert is_salary_window(dt.date(2026, 3, 2))
    assert not is_salary_window(dt.date(2026, 3, 15))
    assert is_gst_window(dt.date(2026, 3, 20))
    assert is_gst_window(dt.date(2026, 3, 7))
    assert not is_gst_window(dt.date(2026, 3, 15))


def test_features_never_look_at_the_future(series) -> None:
    """Every lag reaches backwards. A single forward-looking feature would make the
    whole backtest meaningless."""
    features, targets, dates = build_matrix(series, horizon=1)
    assert len(features) == len(targets) == len(dates)
    assert features.shape[1] == len(calendar_features(dates[0])) + 8


def test_future_features_need_no_future_data(series) -> None:
    features, dates = future_features(series, 14)
    assert features.shape[0] == 14
    assert dates[0] == series.dates[-1] + dt.timedelta(days=1)
    assert np.isfinite(features).all()


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def test_mase_is_about_one_when_the_model_is_the_naive_forecast() -> None:
    """The scale is the in-sample seasonal-naive error, so a naive forecast scores
    near 1. Built with noise on purpose: a perfectly periodic series has *zero* naive
    error, which makes MASE 0/0 - the guard returns 0, and asserting against that
    would be testing the guard rather than the metric."""
    rng = np.random.default_rng(0)
    training = [float(i % 7) * 100 + float(rng.normal(0, 25)) for i in range(70)]
    actual = training[-14:]
    naive = training[-21:-7]
    value = mase(actual, naive, training)
    assert Decimal("0.3") < value < Decimal("3.0"), value


def test_mase_handles_a_series_full_of_zeros() -> None:
    """Where MAPE would be undefined."""
    training = [0.0, 100.0] * 40
    assert mase([0.0, 100.0], [0.0, 100.0], training) == 0


def test_pinball_penalises_the_two_sides_asymmetrically() -> None:
    """The whole reason it scores a band rather than a point."""
    under = pinball([100.0], [50.0], 0.95)
    over = pinball([100.0], [150.0], 0.95)
    assert under > over, "missing high on a 95th percentile must cost more"


# --------------------------------------------------------------------------- #
# The liquidity alert
# --------------------------------------------------------------------------- #
def test_the_alert_fires_on_the_lower_bound_not_the_point_forecast() -> None:
    """A point forecast is a median. Alerting on it means alerting when there is
    already a 50% chance of a shortfall - and staying silent when the band is wide."""
    intervals = [Interval(dt.date(2026, 3, 20), 600_000, 200_000, 1_000_000)]
    commitment = Commitment(
        dt.date(2026, 3, 20), Money(500_000), CommitmentKind.PAYROLL, "payroll"
    )
    alerts = check(intervals, [commitment])
    assert alerts, "a comfortable-looking point forecast hid a real risk"
    assert alerts[0].severity is Severity.WARN
    assert "exactly why alerting on it would have stayed silent" in alerts[0].reason


def test_no_alert_when_the_lower_bound_clears_comfortably() -> None:
    intervals = [Interval(dt.date(2026, 3, 20), 1_000_000, 900_000, 1_100_000)]
    commitment = Commitment(
        dt.date(2026, 3, 20), Money(100_000), CommitmentKind.VENDOR, "vendor"
    )
    assert check(intervals, [commitment]) == []


def test_a_shortfall_in_the_point_forecast_is_critical() -> None:
    intervals = [Interval(dt.date(2026, 3, 20), 100_000, 50_000, 150_000)]
    commitment = Commitment(
        dt.date(2026, 3, 20), Money(500_000), CommitmentKind.PAYROLL, "payroll"
    )
    alerts = check(intervals, [commitment])
    assert alerts[0].severity is Severity.CRITICAL
    assert "not a risk, it is a plan" in alerts[0].reason


def test_a_thin_cushion_produces_a_quieter_watch() -> None:
    intervals = [Interval(dt.date(2026, 3, 20), 520_000, 505_000, 540_000)]
    commitment = Commitment(
        dt.date(2026, 3, 20), Money(500_000), CommitmentKind.PAYROLL, "payroll"
    )
    alerts = check(intervals, [commitment])
    assert alerts[0].severity is Severity.WATCH


def test_commitments_accumulate_cash_up_to_the_due_date() -> None:
    """A commitment on the 20th is met by everything landing up to the 20th."""
    intervals = [
        Interval(dt.date(2026, 3, 18), 100, 80, 120),
        Interval(dt.date(2026, 3, 19), 100, 80, 120),
        Interval(dt.date(2026, 3, 25), 100, 80, 120),
    ]
    expected, confident = cumulative_by(intervals, dt.date(2026, 3, 20))
    assert expected.paise == 200
    assert confident.paise == 160


def test_statutory_commitments_get_a_different_proposal() -> None:
    intervals = [Interval(dt.date(2026, 3, 20), 10_000, 5_000, 15_000)]
    gst = Commitment(dt.date(2026, 3, 20), Money(500_000), CommitmentKind.GST, "GST")
    alerts = check(intervals, [gst])
    assert "cannot be delayed" in alerts[0].proposal


def test_every_proposal_is_for_a_human(forecast) -> None:
    """Triveni proposes; it never moves money. There is no path from an alert to a
    transfer because there is no transfer capability to reach."""
    for alert in forecast.alerts:
        assert alert.proposal
        assert "human decides" in alert.proposal or "Arrange" in alert.proposal or "Re-check" in alert.proposal


def test_default_commitments_cover_the_indian_monthly_cycle() -> None:
    commitments = default_commitments(dt.date(2026, 3, 1), 40, Money.from_rupees("500000"))
    kinds = {c.kind for c in commitments}
    assert CommitmentKind.PAYROLL in kinds
    assert CommitmentKind.GST in kinds
    assert CommitmentKind.TDS in kinds


# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #
def test_too_little_history_refuses_rather_than_guessing() -> None:
    """A number with a wide band on it is not better than saying there is not enough
    data."""
    payload = forecast_cash(ROOT / "data" / "seed", horizon_days=14)
    assert not payload["ok"]
    assert "not enough" in payload["reason"]


def test_the_mcp_tool_reports_coverage_and_the_winning_model(forecast) -> None:
    payload = forecast_cash(HISTORY, horizon_days=14)
    assert payload["ok"]
    assert payload["coverage"]["nominal"] and payload["coverage"]["realised"]
    assert payload["model"] in {"seasonal_naive", "seasonal_drift", "theta", "ets", "boosted"}
    assert "rolling-origin" in payload["reason"]
    assert "lower bound" in payload["reason"]


def test_the_forecast_is_deterministic() -> None:
    first, second = build(HISTORY), build(HISTORY)
    assert [i.canonical() for i in first.intervals] == [i.canonical() for i in second.intervals]
    assert first.coverage.canonical() == second.coverage.canonical()


def test_the_forecast_predicts_near_zero_on_closed_days(forecast) -> None:
    """Structural, not statistical: the bank is shut, so nothing lands."""
    closed = [
        interval
        for interval in forecast.intervals
        if not INDIAN_BANK_CALENDAR.is_business_day(interval.date)
    ]
    assert closed, "the horizon should contain at least one closed day"
    open_days = [i for i in forecast.intervals if INDIAN_BANK_CALENDAR.is_business_day(i.date)]
    assert np.mean([i.point for i in closed]) < np.mean([i.point for i in open_days]) / 5
