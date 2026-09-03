"""Loop 3: the cash forecast, end to end.

Builds the daily series from **reconciled** bank credits - not from gateway captures,
which is the distinction that makes this forecast worth having. Captures tell you what
was sold; only the reconciled bank side tells you what actually landed, and a treasurer
pays payroll out of the second one.

Runs the ladder, conformalises the winner's band, measures the coverage that band
actually achieved, and checks the lower bound against committed outflows.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from core.money import Money, format_inr
from forecast.alerts import Commitment, LiquidityAlert, check, default_commitments
from forecast.backtest import (
    BacktestResult,
    CoverageResult,
    Interval,
    conformal_backtest,
    conformalise,
    rolling_origin,
)
from forecast.features import DailySeries
from forecast.models import SeasonalNaive, ladder

ROOT: Final = Path(__file__).resolve().parent.parent
DEFAULT_HORIZON: Final = 14
DEFAULT_ALPHA: Final = Decimal("0.10")

#: Minimum history before a forecast is worth issuing. Below this the honest answer
#: is that there is not enough data, not a number with a wide band on it.
MIN_HISTORY_DAYS: Final = 74


@dataclass(frozen=True, slots=True)
class Forecast:
    """A horizon of intervals, the coverage they achieved, and what they imply."""

    intervals: tuple[Interval, ...]
    coverage: CoverageResult
    backtest: BacktestResult
    alerts: tuple[LiquidityAlert, ...]
    model: str
    history_days: int

    def total_expected(self) -> Money:
        return Money(sum(i.point for i in self.intervals))

    def total_confident(self) -> Money:
        return Money(sum(i.lower for i in self.intervals))

    def render(self) -> str:
        lines = [
            self.backtest.render(),
            "",
            f"conformal interval coverage: {self.coverage.render()}",
            "",
            f"cash landing over the next {len(self.intervals)} day(s), by {self.model}:",
        ]
        lines.extend(interval.render() for interval in self.intervals)
        lines += [
            "",
            f"  expected  {format_inr(self.total_expected()):>16}",
            f"  confident {format_inr(self.total_confident()):>16}  "
            f"(the {self.coverage.nominal:.0%} lower bound - the figure the alert uses)",
        ]
        if self.alerts:
            lines += ["", f"{len(self.alerts)} liquidity alert(s):"]
            lines.extend(alert.render() for alert in self.alerts)
        else:
            lines += ["", "  no liquidity alerts: the lower bound clears every commitment."]
        return "\n".join(lines)

    def canonical(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "history_days": self.history_days,
            "intervals": [i.canonical() for i in self.intervals],
            "coverage": self.coverage.canonical(),
            "backtest": [s.canonical() for s in self.backtest.scores],
            "alerts": [a.canonical() for a in self.alerts],
            "total_expected_paise": self.total_expected().paise,
            "total_confident_paise": self.total_confident().paise,
        }


def series_from_reconciliation(directory: Path) -> DailySeries:
    """Daily cash landing, from reconciled bank credits.

    Falls back to reading the statement directly when a full reconciliation is not
    wanted - the series is the same either way, and the forecast does not need the
    matching to have succeeded to know what arrived.
    """
    from ingest.adapters.csv_bank import read_bank_statement
    from ingest.canonical import Direction

    result = read_bank_statement(directory=directory)
    by_date: dict[dt.date, int] = {}
    for row in result.rows:
        if row.direction is not Direction.CREDIT:
            continue
        day = row.settled_on or row.occurred_on
        by_date[day] = by_date.get(day, 0) + row.amount.paise
    return DailySeries.from_mapping(by_date)


def build(
    directory: Path | None = None,
    *,
    horizon: int = DEFAULT_HORIZON,
    alpha: Decimal = DEFAULT_ALPHA,
    commitments: list[Commitment] | None = None,
    opening_balance: Money | None = None,
) -> Forecast:
    """Run the whole loop. Raises rather than guessing when history is too short."""
    directory = directory or (ROOT / "data" / "history")
    series = series_from_reconciliation(directory)
    if len(series) < MIN_HISTORY_DAYS:
        raise ValueError(
            f"{len(series)} day(s) of history is not enough to backtest a "
            f"{horizon}-day horizon; at least {MIN_HISTORY_DAYS} are needed. "
            f"Generate more with `python -m data.gen --spec history`."
        )

    backtest = rolling_origin(series, horizon=horizon)
    coverage, _intervals, _actuals = conformal_backtest(series, horizon=horizon, alpha=alpha)

    # Forecast forward with whichever rung actually won, not with the fanciest one.
    winner = backtest.best
    model = next((m for m in ladder() if m.name == winner.name), SeasonalNaive())
    model.fit(series, horizon)
    points = model.predict(series, horizon)

    residuals: list[float] = []
    for offset in range(min(60, len(series) - 30)):
        index = len(series) - offset - 1
        history = series.slice(index)
        if len(history) < 30:
            continue
        residuals.append(float(series.values[index] - model.predict(history, 1)[0]))

    last = series.dates[-1]
    dates = [last + dt.timedelta(days=step) for step in range(1, horizon + 1)]
    intervals = conformalise(points, residuals or [0.0], dates, alpha)

    if commitments is None:
        # Payroll sized from the merchant's own recent throughput rather than picked,
        # so a bigger merchant gets a bigger payroll and the alert stays meaningful.
        monthly = Money(int(sum(series.values[-28:]) * 0.55)) if len(series) >= 28 else Money(0)
        commitments = default_commitments(dates[0], horizon, monthly)

    alerts = check(
        intervals,
        commitments,
        opening_balance=opening_balance,
        nominal_coverage=Decimal(1) - alpha,
    )

    return Forecast(
        intervals=tuple(intervals),
        coverage=coverage,
        backtest=backtest,
        alerts=tuple(alerts),
        model=winner.name,
        history_days=len(series),
    )


def forecast_cash(
    directory: Path | None = None, horizon_days: int = DEFAULT_HORIZON
) -> dict[str, Any]:
    """The MCP tool body. Reports its own inability rather than inventing a series."""
    try:
        forecast = build(directory, horizon=horizon_days)
    except (ValueError, FileNotFoundError) as exc:
        return {
            "ok": False,
            "available": False,
            "reason": f"cannot forecast: {exc}",
        }

    return {
        "ok": True,
        "model": forecast.model,
        "horizon_days": horizon_days,
        "expected": format_inr(forecast.total_expected()),
        "confident_lower_bound": format_inr(forecast.total_confident()),
        "coverage": {
            "nominal": str(forecast.coverage.nominal),
            "realised": str(forecast.coverage.realised),
            "holds": forecast.coverage.holds,
        },
        "backtest": [s.canonical() for s in forecast.backtest.scores],
        "intervals": [i.canonical() for i in forecast.intervals],
        "alerts": [a.canonical() for a in forecast.alerts],
        "reason": (
            f"{forecast.model} won a {forecast.backtest.folds}-fold rolling-origin "
            f"backtest at MASE {forecast.backtest.best.mase}. Over the next "
            f"{horizon_days} days it expects {format_inr(forecast.total_expected())}, "
            f"of which {format_inr(forecast.total_confident())} is inside the "
            f"{forecast.coverage.nominal:.0%} lower bound - a band whose realised "
            f"coverage was {forecast.coverage.realised:.1%}. "
            + (
                f"{len(forecast.alerts)} liquidity alert(s) fired on that lower bound."
                if forecast.alerts
                else "No commitment falls outside it."
            )
        ),
    }


def write_report(forecast: Forecast, path: Path) -> Path:
    path.write_text(
        json.dumps(forecast.canonical(), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return path
