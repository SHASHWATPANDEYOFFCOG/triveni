"""A ladder of forecasters, every rung scored on the same backtest.

The point of a ladder is that the top rung has to *earn* its place. A gradient-boosted
model that cannot beat "last Tuesday" is not a model, it is a liability with a
`fit` method - and the only way to know which you have is to score them identically.

    seasonal naive   last week's same weekday. Free, and genuinely hard to beat.
    drift            seasonal naive plus a trend term.
    theta            the Theta method, a strong classical benchmark.
    ets              Holt-Winters with additive weekly seasonality.
    boosted          gradient-boosted quantile regression on calendar and lag features.

**Scored with MASE and pinball loss, not MAPE.** MAPE is undefined on a zero-cash day,
and an Indian merchant's series is *full* of structural zeros - every Sunday, every 2nd
and 4th Saturday. A metric that cannot be computed on a third of the series is not a
metric. MASE scales the error by the in-sample seasonal-naive error, so 1.0 means "no
better than last Tuesday" and anything above it means worse. Pinball loss scores the
*intervals* rather than the point, which is what actually matters for a liquidity
decision: being wrong about the middle is survivable, being wrong about the floor is not.

**Rolling origin, never a single split.** One train/test split on a time series measures
how well you did on one particular fortnight. Rolling origin re-fits at each cut-off
and forecasts forward, which is the only honest simulation of using the thing.

If the boosted model loses to seasonal naive, that gets published. A repository whose
fanciest model is beaten by a one-line baseline and says so is more trustworthy than
one where it mysteriously always wins.
"""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final, Protocol

import numpy as np

from forecast.features import (
    DailySeries,
    build_matrix,
    future_features,
    seasonal_period,
)

#: Quantiles the interval models are fitted at. 0.5 is the point forecast; the outer
#: pair is the raw band before conformalisation widens it to its measured coverage.
QUANTILES: Final[tuple[float, ...]] = (0.05, 0.5, 0.95)

SEED: Final = 20260101


class Forecaster(Protocol):
    name: str
    description: str

    def fit(self, series: DailySeries, horizon: int) -> Forecaster: ...
    def predict(self, series: DailySeries, horizon: int) -> np.ndarray: ...


# --------------------------------------------------------------------------- #
# Baselines
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class SeasonalNaive:
    """Last week's same weekday. The rung everything else has to beat."""

    name: str = "seasonal_naive"
    description: str = "value from the same weekday one week ago"
    period: int = field(default_factory=seasonal_period)

    def fit(self, series: DailySeries, horizon: int) -> SeasonalNaive:
        return self

    def predict(self, series: DailySeries, horizon: int) -> np.ndarray:
        values = series.as_array()
        if len(values) < self.period:
            return np.repeat(values[-1] if len(values) else 0.0, horizon)
        return np.array(
            [values[-self.period + (step % self.period)] for step in range(horizon)],
            dtype=np.float64,
        )


@dataclass(slots=True)
class SeasonalDrift:
    """Seasonal naive plus the average change per period. Catches slow growth."""

    name: str = "seasonal_drift"
    description: str = "seasonal naive plus the average week-on-week change"
    period: int = field(default_factory=seasonal_period)

    def fit(self, series: DailySeries, horizon: int) -> SeasonalDrift:
        return self

    def predict(self, series: DailySeries, horizon: int) -> np.ndarray:
        values = series.as_array()
        base = SeasonalNaive(period=self.period).predict(series, horizon)
        if len(values) < 2 * self.period:
            return base
        recent = values[-self.period :].mean()
        previous = values[-2 * self.period : -self.period].mean()
        drift = (recent - previous) / self.period
        return base + drift * np.arange(1, horizon + 1)


@dataclass(slots=True)
class Theta:
    """The Theta method: a classical benchmark that is genuinely hard to beat.

    Implemented directly - deseasonalise, average a linear trend with simple
    exponential smoothing, reseasonalise - because owning it means the comparison is
    ours end to end rather than a library's.
    """

    name: str = "theta"
    description: str = "Theta method on the deseasonalised series"
    period: int = field(default_factory=seasonal_period)
    alpha: float = 0.3

    def fit(self, series: DailySeries, horizon: int) -> Theta:
        return self

    def predict(self, series: DailySeries, horizon: int) -> np.ndarray:
        values = series.as_array()
        if len(values) < 2 * self.period:
            return SeasonalNaive(period=self.period).predict(series, horizon)

        # Multiplicative weekly indices, guarded against the structural zeros.
        overall = values.mean() or 1.0
        indices = np.ones(self.period)
        for phase in range(self.period):
            phase_values = values[phase :: self.period]
            if len(phase_values):
                indices[phase] = (phase_values.mean() or overall) / overall
        indices = np.where(indices <= 0, 1e-6, indices)

        phases = np.arange(len(values)) % self.period
        deseasonalised = values / indices[phases]

        times = np.arange(len(deseasonalised), dtype=np.float64)
        slope, intercept = np.polyfit(times, deseasonalised, 1)

        level = deseasonalised[0]
        for value in deseasonalised[1:]:
            level = self.alpha * value + (1 - self.alpha) * level

        out = np.zeros(horizon)
        for step in range(1, horizon + 1):
            linear = intercept + slope * (len(deseasonalised) + step - 1)
            combined = 0.5 * linear + 0.5 * level
            phase = (len(values) + step - 1) % self.period
            out[step - 1] = combined * indices[phase]
        return out


@dataclass(slots=True)
class ETS:
    """Holt-Winters with additive weekly seasonality, via statsmodels.

    Falls back to Theta when the series is too short or the fit does not converge -
    reported as a fallback rather than silently substituted, so the ladder table never
    credits ETS with a score Theta produced.
    """

    name: str = "ets"
    description: str = "Holt-Winters, additive trend and weekly seasonality"
    period: int = field(default_factory=seasonal_period)
    _fallback: bool = False

    def fit(self, series: DailySeries, horizon: int) -> ETS:
        return self

    def predict(self, series: DailySeries, horizon: int) -> np.ndarray:
        values = series.as_array()
        if len(values) < 3 * self.period:
            self._fallback = True
            return Theta(period=self.period).predict(series, horizon)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                from statsmodels.tsa.holtwinters import ExponentialSmoothing

                fitted = ExponentialSmoothing(
                    values,
                    trend="add",
                    seasonal="add",
                    seasonal_periods=self.period,
                    initialization_method="estimated",
                ).fit()
                self._fallback = False
                return np.asarray(fitted.forecast(horizon), dtype=np.float64)
        except (ValueError, TypeError, np.linalg.LinAlgError):
            self._fallback = True
            return Theta(period=self.period).predict(series, horizon)


# --------------------------------------------------------------------------- #
# Boosted quantile regression
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class BoostedQuantile:
    """Gradient-boosted trees, one model per horizon per quantile.

    Direct multi-horizon: a separate fit for each step ahead, so day-fourteen never
    consumes day-one's prediction and errors do not compound. Quantile loss rather
    than squared error, because the lower band is the number a liquidity decision
    actually turns on and squared error would optimise the middle at its expense.
    """

    name: str = "boosted"
    description: str = "gradient-boosted quantile regression on calendar and lag features"
    quantiles: tuple[float, ...] = QUANTILES
    max_iter: int = 60
    _models: dict[tuple[int, float], Any] = field(default_factory=dict)

    def fit(
        self, series: DailySeries, horizon: int, *, quantiles: tuple[float, ...] | None = None
    ) -> BoostedQuantile:
        """Fit one model per (step, quantile).

        ``quantiles`` narrows the work to what the caller will actually ask for. That
        matters: a full fit is 14 horizons x 3 quantiles = 42 gradient-boosted models,
        and the conformal backtest only ever needs the median, because its band comes
        from held-out residuals rather than from the model's own quantiles. Fitting all
        three there was tripling the most expensive step in the system for output
        nothing read.
        """
        from sklearn.ensemble import HistGradientBoostingRegressor

        wanted = quantiles if quantiles is not None else self.quantiles
        self._models = {}
        for step in range(1, horizon + 1):
            features, targets, _dates = build_matrix(series, horizon=step)
            if len(targets) < 20:
                continue
            for quantile in wanted:
                model = HistGradientBoostingRegressor(
                    loss="quantile",
                    quantile=quantile,
                    max_iter=self.max_iter,
                    learning_rate=0.10,
                    max_depth=4,
                    min_samples_leaf=8,
                    l2_regularization=1.0,
                    # 32 bins, not the default 255: with ~175 training rows the
                    # default binning is pure overhead and buys no resolution.
                    max_bins=32,
                    random_state=SEED,
                )
                model.fit(features, targets)
                self._models[step, quantile] = model
        return self

    def predict(self, series: DailySeries, horizon: int) -> np.ndarray:
        return self.predict_quantile(series, horizon, 0.5)

    def predict_quantile(self, series: DailySeries, horizon: int, quantile: float) -> np.ndarray:
        features, _dates = future_features(series, horizon)
        out = np.zeros(horizon)
        fallback = SeasonalNaive().predict(series, horizon)
        for step in range(1, horizon + 1):
            model = self._models.get((step, quantile))
            if model is None:
                out[step - 1] = fallback[step - 1]
                continue
            out[step - 1] = float(model.predict(features[step - 1 : step])[0])
        return out

    @property
    def fitted(self) -> bool:
        return bool(self._models)


def ladder() -> list[Forecaster]:
    """Every rung, cheapest first. The order is the argument."""
    return [SeasonalNaive(), SeasonalDrift(), Theta(), ETS(), BoostedQuantile()]


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def mase(actual: Sequence[float], predicted: Sequence[float], training: Sequence[float]) -> Decimal:
    """Mean absolute scaled error, scaled by the in-sample seasonal-naive error.

    MAPE is not used anywhere in this file. It is undefined on a zero-cash day, and an
    Indian merchant's series is full of structural zeros - every Sunday and every 2nd
    and 4th Saturday. A metric that cannot be computed on a third of the series is not
    a metric.

    1.0 means "no better than predicting last Tuesday". Above 1.0 means worse.
    """
    actual_array = np.asarray(actual, dtype=np.float64)
    predicted_array = np.asarray(predicted, dtype=np.float64)
    training_array = np.asarray(training, dtype=np.float64)
    period = seasonal_period()
    if len(training_array) <= period:
        return Decimal(0)
    scale = np.mean(np.abs(training_array[period:] - training_array[:-period]))
    if scale == 0:
        return Decimal(0)
    value = float(np.mean(np.abs(actual_array - predicted_array)) / scale)
    return Decimal(str(round(value, 6)))


def pinball(actual: Sequence[float], predicted: Sequence[float], quantile: float) -> Decimal:
    """Pinball (quantile) loss. Scores the band, not the middle.

    Being wrong about the centre of a cash forecast is survivable; being wrong about
    the floor is what causes a bounced payroll, and pinball loss is the metric that
    knows the difference.
    """
    actual_array = np.asarray(actual, dtype=np.float64)
    predicted_array = np.asarray(predicted, dtype=np.float64)
    delta = actual_array - predicted_array
    loss = np.maximum(quantile * delta, (quantile - 1) * delta)
    return Decimal(str(round(float(np.mean(loss)), 6)))
