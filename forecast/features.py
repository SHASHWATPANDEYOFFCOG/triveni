"""Calendar and lag features for cash landing on a given day.

Cash arriving in an Indian merchant's bank account is not a smooth series. It is a
*calendar* series, and almost all of its structure is things a date knows about itself:

* **the bank is shut.** Nothing lands on a Sunday, or on the 2nd and 4th Saturday, so a
  model without the settlement calendar spends its capacity learning to predict zero on
  days that are structurally zero - and then smears that zero across the days either
  side.
* **weekday effects.** A Monday carries Friday's and the weekend's captures. That is not
  a trend, it is a pile-up, and it recurs weekly.
* **salary days.** Consumer spend in India lifts sharply in the first few days of the
  month and again around the last working day, when salaries land.
* **statutory dates.** GST returns are due on the 20th and TDS deposits by the 7th, so
  a merchant's *outflows* cluster there even when inflows do not - which is exactly
  when a liquidity gap bites.

All of these are known in advance for any future date, which is what makes them usable
for forecasting rather than merely for explanation. Nothing here needs the future.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from functools import lru_cache
from typing import Final

import numpy as np

from core.clock import INDIAN_BANK_CALENDAR, SettlementCalendar

#: Statutory outflow dates in the Indian monthly cycle. Inflow models do not need
#: them; the liquidity alert does, because a gap only matters against a commitment.
GST_RETURN_DAY: Final = 20
TDS_DEPOSIT_DAY: Final = 7

#: Lags the model may look at. 1 and 2 carry momentum; 7 and 14 carry the weekly
#: shape; 28 carries the monthly one.
LAGS: Final[tuple[int, ...]] = (1, 2, 3, 7, 14, 28)

FEATURE_NAMES: Final[tuple[str, ...]] = (
    "is_business_day",
    "weekday_mon",
    "weekday_tue",
    "weekday_wed",
    "weekday_thu",
    "weekday_fri",
    "weekday_sat",
    "day_of_month",
    "is_month_start",
    "is_month_end",
    "is_salary_window",
    "is_gst_window",
    "days_since_business_day",
    "business_days_in_week",
    *(f"lag_{lag}" for lag in LAGS),
    "rolling_mean_7",
    "rolling_mean_28",
)


@dataclass(frozen=True, slots=True)
class DailySeries:
    """Cash landing per day, in paise, on a contiguous daily index.

    Contiguous on purpose - including the zeros. A series that silently omits closed
    days teaches a model that consecutive rows are consecutive *business* days, and
    then a three-day weekend looks like a one-day gap.
    """

    dates: tuple[dt.date, ...]
    values: tuple[int, ...]

    def __post_init__(self) -> None:
        if len(self.dates) != len(self.values):
            raise ValueError("dates and values must be the same length")
        for earlier, later in zip(self.dates, self.dates[1:], strict=False):
            if (later - earlier).days != 1:
                raise ValueError(f"series is not contiguous at {earlier} -> {later}")

    def __len__(self) -> int:
        return len(self.dates)

    @classmethod
    def from_mapping(
        cls, by_date: dict[dt.date, int], *, start: dt.date | None = None, end: dt.date | None = None
    ) -> DailySeries:
        """Fill every calendar day between the bounds, zero where nothing landed."""
        if not by_date and (start is None or end is None):
            return cls((), ())
        first = start or min(by_date)
        last = end or max(by_date)
        dates: list[dt.date] = []
        values: list[int] = []
        cursor = first
        while cursor <= last:
            dates.append(cursor)
            values.append(by_date.get(cursor, 0))
            cursor += dt.timedelta(days=1)
        return cls(tuple(dates), tuple(values))

    def as_array(self) -> np.ndarray:
        return np.array(self.values, dtype=np.float64)

    def slice(self, upto: int) -> DailySeries:
        return DailySeries(self.dates[:upto], self.values[:upto])


def is_salary_window(day: dt.date, calendar: SettlementCalendar = INDIAN_BANK_CALENDAR) -> bool:
    """The first three days of a month, or the last working day of the previous one.

    Salaries in India overwhelmingly land at the turn of the month, and consumer spend
    follows within a day or two.
    """
    if day.day <= 3:
        return True
    following = day + dt.timedelta(days=1)
    return following.month != day.month and calendar.is_business_day(day)


def is_gst_window(day: dt.date) -> bool:
    """The days around the 20th, when GST returns are filed and paid."""
    return abs(day.day - GST_RETURN_DAY) <= 1 or day.day == TDS_DEPOSIT_DAY


@lru_cache(maxsize=4096)
def days_since_business_day(day: dt.date, calendar: SettlementCalendar) -> int:
    """How long the bank has been shut running up to this date.

    Zero on an open day, and 2 on the Monday after a 2nd-Saturday weekend - which is
    the single most useful number for explaining why Monday's credit is large.
    """
    count = 0
    cursor = day - dt.timedelta(days=1)
    while not calendar.is_business_day(cursor) and count < 10:
        count += 1
        cursor -= dt.timedelta(days=1)
    return count


@lru_cache(maxsize=4096)
def business_days_in_week(day: dt.date, calendar: SettlementCalendar) -> int:
    monday = day - dt.timedelta(days=day.weekday())
    return sum(
        1 for offset in range(7) if calendar.is_business_day(monday + dt.timedelta(days=offset))
    )


@lru_cache(maxsize=8192)
def _calendar_features_cached(day: dt.date, calendar: SettlementCalendar) -> tuple[float, ...]:
    """Cached because these are pure functions of a date and get asked for the same
    dates thousands of times.

    Direct multi-horizon forecasting builds a design matrix once per horizon step, so
    a 14-day horizon walks the same 175 dates fourteen times, and each walk was
    re-deriving "how long has the bank been shut" by looping back through the
    calendar. That was two thirds of the fit time - 37.5s per fit, of which only 13s
    was actually gradient boosting.
    """
    weekday = day.weekday()
    next_day = day + dt.timedelta(days=1)
    return (
        1.0 if calendar.is_business_day(day) else 0.0,
        *(1.0 if weekday == i else 0.0 for i in range(6)),
        float(day.day),
        1.0 if day.day <= 3 else 0.0,
        1.0 if next_day.month != day.month else 0.0,
        1.0 if is_salary_window(day, calendar) else 0.0,
        1.0 if is_gst_window(day) else 0.0,
        float(days_since_business_day(day, calendar)),
        float(business_days_in_week(day, calendar)),
    )


def calendar_features(
    day: dt.date, calendar: SettlementCalendar = INDIAN_BANK_CALENDAR
) -> list[float]:
    """Everything a date knows about itself. Available for any future date."""
    return list(_calendar_features_cached(day, calendar))


def build_matrix(
    series: DailySeries,
    *,
    calendar: SettlementCalendar = INDIAN_BANK_CALENDAR,
    horizon: int = 1,
) -> tuple[np.ndarray, np.ndarray, list[dt.date]]:
    """Design matrix and target for direct ``horizon``-step-ahead forecasting.

    *Direct* rather than recursive: a separate model per horizon, each predicting
    ``h`` days ahead from information available today. Recursive forecasting feeds a
    model its own predictions and compounds their errors, which on a 14-day horizon
    turns a small day-one bias into a large day-fourteen one.

    Rows whose lags reach before the start of the series are dropped rather than
    imputed - inventing history to fill a lag is how a backtest starts flattering
    itself.
    """
    values = series.as_array()
    max_lag = max(LAGS)
    rows: list[list[float]] = []
    targets: list[float] = []
    row_dates: list[dt.date] = []

    for index in range(max_lag, len(series) - horizon + 1):
        target_index = index + horizon - 1
        if target_index >= len(series):
            break
        target_date = series.dates[target_index]
        features = calendar_features(target_date, calendar)
        features.extend(float(values[index - lag]) for lag in LAGS)
        window7 = values[max(index - 7, 0) : index]
        window28 = values[max(index - 28, 0) : index]
        features.append(float(window7.mean()) if len(window7) else 0.0)
        features.append(float(window28.mean()) if len(window28) else 0.0)
        rows.append(features)
        targets.append(float(values[target_index]))
        row_dates.append(target_date)

    if not rows:
        return np.zeros((0, len(FEATURE_NAMES))), np.zeros(0), []
    return np.array(rows, dtype=np.float64), np.array(targets, dtype=np.float64), row_dates


def future_features(
    series: DailySeries,
    horizon: int,
    *,
    calendar: SettlementCalendar = INDIAN_BANK_CALENDAR,
) -> tuple[np.ndarray, list[dt.date]]:
    """Features for the next ``horizon`` days, from history alone.

    Every lag is anchored at the end of the observed series rather than being rolled
    forward through predictions, which is what keeps this a direct forecast.
    """
    values = series.as_array()
    last = series.dates[-1]
    rows: list[list[float]] = []
    dates: list[dt.date] = []

    for step in range(1, horizon + 1):
        target_date = last + dt.timedelta(days=step)
        features = calendar_features(target_date, calendar)
        features.extend(float(values[-lag]) if len(values) >= lag else 0.0 for lag in LAGS)
        features.append(float(values[-7:].mean()) if len(values) >= 7 else 0.0)
        features.append(float(values[-28:].mean()) if len(values) >= 28 else 0.0)
        rows.append(features)
        dates.append(target_date)

    return np.array(rows, dtype=np.float64), dates


def seasonal_period() -> int:
    """Weekly. The bank calendar makes seven the dominant cycle, not thirty."""
    return 7
