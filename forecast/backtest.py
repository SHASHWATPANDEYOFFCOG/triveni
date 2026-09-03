"""Rolling-origin backtesting, conformalised intervals, and the coverage that resulted.

Two things happen here, and the second is the one that matters.

**Rolling origin.** Re-fit at each cut-off, forecast forward, step the cut-off, repeat.
A single train/test split on a time series measures how well a model did on one
particular fortnight; rolling origin measures how it behaves when you actually use it,
which is the only question worth asking.

**Conformalised intervals with *measured* coverage.** A model's own 90% band is a
statement about its assumptions, not about reality. Split-conformal prediction turns it
into a statement about reality: take the absolute residuals from a held-out calibration
window, find their (1-alpha) quantile, and widen the band by it. The resulting interval
has a distribution-free coverage guarantee under exchangeability.

Then - and this is the part most systems skip - **go and measure what coverage it
actually got**. A 90% band that contains the truth 71% of the time is a lie, and the
only way anyone finds out is if the realised number is printed next to the nominal one.
:class:`CoverageResult` carries both, always, and never one without the other.

Time series are the case where exchangeability is *most* obviously strained: tomorrow
is not exchangeable with last March. So the intervals are calibrated on a *recent*
window rather than the whole history, which is the standard adaptive-conformal response
(Gibbs & Candes; Zaffran et al., ICML 2022) - and the honest caveat stays in the output.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import replace as dc_replace
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final

import numpy as np

from core.money import Money, format_inr
from forecast.features import DailySeries
from forecast.models import (
    BoostedQuantile,
    Forecaster,
    ladder,
    mase,
    pinball,
)

#: How many recent residuals calibrate the interval. Recent rather than all of
#: history, because a series is not exchangeable with its own distant past.
CALIBRATION_WINDOW: Final = 60

MIN_CALIBRATION_RESIDUALS: Final = 20


def _q(value: float | Decimal, places: str = "0.0001") -> Decimal:
    return Decimal(str(value)).quantize(Decimal(places), rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True, slots=True)
class Interval:
    """One day's forecast, with a band that has been measured rather than assumed."""

    date: dt.date
    point: int
    lower: int
    upper: int

    def contains(self, actual: int) -> bool:
        return self.lower <= actual <= self.upper

    def canonical(self) -> dict[str, object]:
        return {
            "date": self.date,
            "point_paise": self.point,
            "lower_paise": self.lower,
            "upper_paise": self.upper,
        }

    def render(self) -> str:
        return (
            f"  {self.date}  {format_inr(Money(self.point)):>14}   "
            f"[{format_inr(Money(self.lower)):>14} .. {format_inr(Money(self.upper)):>14}]"
        )


@dataclass(frozen=True, slots=True)
class CoverageResult:
    """Nominal beside realised. Never one without the other."""

    nominal: Decimal
    realised: Decimal
    n: int
    mean_width_paise: int

    @property
    def holds(self) -> bool:
        """Strictly at or above nominal. Reported as-is, without softening."""
        return self.realised >= self.nominal

    @property
    def standard_error(self) -> Decimal:
        """Binomial standard error of the realised rate at this sample size.

        Reported because "88.1% against a nominal 90%" means something very different
        at n=84 than at n=8,400. At 84 days the standard error is about 3.3 points, so
        88.1% sits well inside one - it is consistent with a band that covers, and
        saying only "UNDER" would overstate the evidence in the other direction.
        """
        if self.n <= 0:
            return Decimal(0)
        p = float(self.nominal)
        return _q((p * (1 - p) / self.n) ** 0.5)

    @property
    def within_noise(self) -> bool:
        """True when the shortfall is inside one standard error."""
        return self.realised >= self.nominal - self.standard_error

    @property
    def gap(self) -> Decimal:
        return _q(self.realised - self.nominal)

    def render(self) -> str:
        if self.holds:
            verdict = "ok"
        elif self.within_noise:
            verdict = f"UNDER by {abs(self.gap):.1%}, inside 1 SE ({self.standard_error:.1%})"
        else:
            verdict = f"UNDER by {abs(self.gap):.1%}, beyond 1 SE ({self.standard_error:.1%})"
        return (
            f"nominal {self.nominal:.1%} · realised {self.realised:.1%} "
            f"over {self.n} day(s), mean width {format_inr(Money(self.mean_width_paise))} "
            f"[{verdict}]"
        )

    def canonical(self) -> dict[str, object]:
        return {
            "nominal": self.nominal,
            "realised": self.realised,
            "n": self.n,
            "mean_width_paise": self.mean_width_paise,
            "holds": self.holds,
            "within_noise": self.within_noise,
            "standard_error": self.standard_error,
        }


@dataclass(frozen=True, slots=True)
class ModelScore:
    """One rung of the ladder, on the same backtest as every other rung."""

    name: str
    description: str
    mase: Decimal
    pinball_05: Decimal
    pinball_50: Decimal
    pinball_95: Decimal
    folds: int
    coverage: CoverageResult | None = None

    naive_mase: Decimal = Decimal(1)
    """The seasonal-naive score on the SAME folds, so the comparison is like for like."""

    @property
    def beats_naive(self) -> bool:
        """Against the baseline's actual score, not against 1.0.

        MASE scales by the *in-sample* naive error, so an out-of-sample naive forecast
        does not score exactly 1.0 - on this data it scores 0.84. Comparing every model
        against 1.0 therefore labelled seasonal_drift (0.93) as 'beats naive' when it
        is materially worse than the naive baseline it is meant to improve on.
        """
        return self.mase < self.naive_mase

    def render(self) -> str:
        verdict = "" if self.name == "seasonal_naive" else (
            "  beats naive" if self.beats_naive else "  LOSES to naive"
        )
        coverage = f"  {self.coverage.realised:.0%}" if self.coverage else "     -"
        return (
            f"  {self.name:16} {self.mase:>8} {self.pinball_50:>14} "
            f"{self.pinball_05:>14}{coverage}{verdict}"
        )

    def canonical(self) -> dict[str, object]:
        return {
            "name": self.name,
            "mase": self.mase,
            "pinball_05": self.pinball_05,
            "pinball_50": self.pinball_50,
            "pinball_95": self.pinball_95,
            "folds": self.folds,
            "coverage": self.coverage.canonical() if self.coverage else None,
        }


# --------------------------------------------------------------------------- #
# Conformalising a band
# --------------------------------------------------------------------------- #
def conformal_width(residuals: Sequence[float], alpha: Decimal) -> float:
    """The (1-alpha) quantile of absolute residuals, with the finite-sample bump.

    ``ceil((n+1)(1-alpha))/n`` rather than the plain quantile: the correction is what
    makes the resulting interval a bound rather than an in-sample observation, and at
    small n it pushes the quantile past 1.0, at which point the widest observed
    residual is used and the method has correctly said "I cannot be more precise than
    my worst miss".
    """
    if len(residuals) < MIN_CALIBRATION_RESIDUALS:
        return float(np.max(np.abs(residuals))) if len(residuals) else 0.0
    absolute = np.abs(np.asarray(residuals, dtype=np.float64))
    n = len(absolute)
    level = min(float(np.ceil((n + 1) * (1 - float(alpha)))) / n, 1.0)
    return float(np.quantile(absolute, level))


def conformalise(
    points: Sequence[float],
    residuals: Sequence[float] | dict[int, Sequence[float]],
    dates: Sequence[dt.date],
    alpha: Decimal = Decimal("0.10"),
) -> list[Interval]:
    """Widen a point forecast into a band whose coverage is guaranteed, not hoped for.

    ``residuals`` may be a flat sequence or, better, a mapping from horizon step to
    that step's residuals. **Per-step calibration is not a refinement, it is the
    difference between a band that works and one that does not.** Forecast error grows
    with horizon: sizing a 14-day band from one-step-ahead residuals under-covers
    badly at the far end, and the first implementation did exactly that and realised
    77.4% coverage against a nominal 90%.

    Cash cannot be negative, so the lower bound is floored at zero - a real constraint
    rather than a cosmetic one, because a liquidity alert that fires on a band dipping
    to minus four lakh is an alert nobody trusts twice.
    """
    if isinstance(residuals, dict):
        widths = {
            step: conformal_width(values, alpha) for step, values in sorted(residuals.items())
        }
        fallback = max(widths.values()) if widths else 0.0
    else:
        flat = conformal_width(residuals, alpha)
        widths, fallback = {}, flat

    out: list[Interval] = []
    for step, (point, date) in enumerate(zip(points, dates, strict=True), start=1):
        width = widths.get(step, fallback)
        out.append(
            Interval(
                date=date,
                point=int(round(point)),
                lower=max(int(round(point - width)), 0),
                upper=int(round(point + width)),
            )
        )
    return out


def measure_coverage(
    intervals: Sequence[Interval], actuals: Sequence[int], alpha: Decimal
) -> CoverageResult:
    """What the band actually caught. The number that keeps the guarantee honest."""
    if not intervals:
        return CoverageResult(Decimal(1) - alpha, Decimal(0), 0, 0)
    hits = sum(1 for interval, actual in zip(intervals, actuals, strict=True) if interval.contains(actual))
    widths = [interval.upper - interval.lower for interval in intervals]
    return CoverageResult(
        nominal=_q(Decimal(1) - alpha),
        realised=_q(Decimal(hits) / Decimal(len(intervals))),
        n=len(intervals),
        mean_width_paise=int(round(sum(widths) / len(widths))),
    )


# --------------------------------------------------------------------------- #
# Rolling origin
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class BacktestResult:
    scores: tuple[ModelScore, ...]
    horizon: int
    folds: int
    train_start: dt.date
    test_end: dt.date

    @property
    def best(self) -> ModelScore:
        return min(self.scores, key=lambda s: (s.mase, s.name))

    def render(self) -> str:
        lines = [
            f"rolling-origin backtest: {self.folds} fold(s), horizon {self.horizon} day(s)",
            f"  {self.train_start} .. {self.test_end}",
            "",
            f"  {'model':16} {'MASE':>8} {'pinball@50':>14} {'pinball@05':>14}  cov",
            "  " + "-" * 62,
        ]
        lines.extend(score.render() for score in self.scores)
        lines.append("")
        best = self.best
        if best.name == "seasonal_naive":
            lines.append(
                "  The seasonal naive baseline wins. Published as-is: a boosted model "
                "that\n  cannot beat 'last Tuesday' has not earned its place, and "
                "saying so is\n  worth more than a table where the fanciest model "
                "always wins."
            )
        else:
            lines.append(
                f"  {best.name} wins at MASE {best.mase} "
                f"({(1 - best.mase) * 100:.1f}% better than seasonal naive)."
            )
        return "\n".join(lines)


def rolling_origin(
    series: DailySeries,
    *,
    horizon: int = 14,
    folds: int = 6,
    step: int = 7,
    models: Sequence[Forecaster] | None = None,
) -> BacktestResult:
    """Re-fit at each cut-off and forecast forward. Never a single split.

    Every model sees exactly the same folds and the same training windows, which is
    the only way the comparison means anything.
    """
    models = list(models or ladder())
    minimum_train = 60
    if len(series) < minimum_train + horizon:
        raise ValueError(
            f"need at least {minimum_train + horizon} days to backtest a "
            f"{horizon}-day horizon; got {len(series)}"
        )

    cutoffs: list[int] = []
    cursor = len(series) - horizon
    for _ in range(folds):
        if cursor < minimum_train:
            break
        cutoffs.append(cursor)
        cursor -= step
    cutoffs.reverse()

    per_model: dict[str, dict[str, list[float]]] = {
        model.name: {"actual": [], "point": [], "low": [], "high": []} for model in models
    }

    for cutoff in cutoffs:
        train = series.slice(cutoff)
        actual = list(series.values[cutoff : cutoff + horizon])
        for model in models:
            model.fit(train, horizon)
            point = model.predict(train, horizon)
            if isinstance(model, BoostedQuantile) and model.fitted:
                low = model.predict_quantile(train, horizon, 0.05)
                high = model.predict_quantile(train, horizon, 0.95)
            else:
                low = point
                high = point
            bucket = per_model[model.name]
            bucket["actual"].extend(float(v) for v in actual)
            bucket["point"].extend(float(v) for v in point)
            bucket["low"].extend(float(v) for v in low)
            bucket["high"].extend(float(v) for v in high)

    training_values = series.values[: cutoffs[0]] if cutoffs else series.values
    scores: list[ModelScore] = []
    for model in models:
        bucket = per_model[model.name]
        if not bucket["actual"]:
            continue
        scores.append(
            ModelScore(
                name=model.name,
                description=model.description,
                mase=mase(bucket["actual"], bucket["point"], training_values),
                pinball_05=pinball(bucket["actual"], bucket["low"], 0.05),
                pinball_50=pinball(bucket["actual"], bucket["point"], 0.5),
                pinball_95=pinball(bucket["actual"], bucket["high"], 0.95),
                folds=len(cutoffs),
            )
        )

    # dataclasses.replace, not **__dict__: ModelScore uses slots, so it has no
    # __dict__ and the dict-splat version silently returned every score unchanged -
    # leaving seasonal_drift labelled "beats naive" when it is worse than naive.
    naive = next((item for item in scores if item.name == "seasonal_naive"), None)
    if naive is not None:
        scores = [dc_replace(item, naive_mase=naive.mase) for item in scores]

    return BacktestResult(
        scores=tuple(scores),
        horizon=horizon,
        folds=len(cutoffs),
        train_start=series.dates[0],
        test_end=series.dates[-1],
    )


def _fit_median(model: Forecaster, series: DailySeries, horizon: int) -> None:
    """Fit only what the caller will read. Baselines ignore the hint."""
    if isinstance(model, BoostedQuantile):
        model.fit(series, horizon, quantiles=(0.5,))
    else:
        model.fit(series, horizon)


def conformal_backtest(
    series: DailySeries,
    *,
    horizon: int = 14,
    folds: int = 6,
    step: int = 7,
    alpha: Decimal = Decimal("0.10"),
    model: Forecaster | None = None,
) -> tuple[CoverageResult, list[Interval], list[int]]:
    """Measure what coverage the conformalised band actually achieves.

    Residuals for calibration come from a window *before* each cut-off, so nothing the
    band is scored on has been seen while the band was being sized.
    """
    model = model or BoostedQuantile()
    all_intervals: list[Interval] = []
    all_actuals: list[int] = []

    cutoffs: list[int] = []
    cursor = len(series) - horizon
    for _ in range(folds):
        if cursor < 60:
            break
        cutoffs.append(cursor)
        cursor -= step
    cutoffs.reverse()

    for cutoff in cutoffs:
        train = series.slice(cutoff)

        # TRUE split conformal: fit on the first part of the training window, take
        # residuals on the part the model has not seen, then re-fit on everything for
        # the actual forecast.
        #
        # Taking residuals from data the model was fitted on is the classic conformal
        # mistake, and a boosted model makes it especially bad because it fits its own
        # training rows well - so the residuals understate the error the band has to
        # cover. That version realised 77.4% coverage against a 90% nominal.
        #
        # Residuals are also kept PER HORIZON STEP, because forecast error grows with
        # horizon and one pooled set sizes day 14's band as if it were day 1's.
        fit_end = int(len(train) * 0.75)
        residuals: dict[int, list[float]] = {step: [] for step in range(1, horizon + 1)}
        if fit_end >= 40:
            calibration_model = type(model)()
            # Median only: the band here comes from held-out residuals, not from the
            # model's own quantiles, so fitting the 5th and 95th would be two thirds
            # of the work for output nothing reads.
            _fit_median(calibration_model, train.slice(fit_end), horizon)
            for index in range(fit_end, len(train) - horizon + 1):
                history = train.slice(index)
                predicted = calibration_model.predict(history, horizon)
                for step in range(1, horizon + 1):
                    residuals[step].append(
                        float(train.values[index + step - 1] - predicted[step - 1])
                    )

        _fit_median(model, train, horizon)
        points = model.predict(train, horizon)
        dates = [series.dates[cutoff + i] for i in range(horizon)]
        usable = {k: v for k, v in residuals.items() if v}
        intervals = conformalise(points, usable or [0.0], dates, alpha)
        all_intervals.extend(intervals)
        all_actuals.extend(series.values[cutoff : cutoff + horizon])

    return measure_coverage(all_intervals, all_actuals, alpha), all_intervals, all_actuals
