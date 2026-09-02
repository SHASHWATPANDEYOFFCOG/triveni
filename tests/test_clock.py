"""M1 gate for time: injectable clocks, no naive datetimes, a real bank calendar.

The settlement calendar is worth testing hard because it is the single largest source
of *phantom* exceptions. If T+2 is computed with a naive date subtraction, every long
weekend manufactures a wave of `timing` breaks that a human then reconciles by hand -
which is exactly the work Triveni exists to remove.
"""

from __future__ import annotations

import datetime as dt

import pytest
from hypothesis import given
from hypothesis import strategies as st

from core.clock import (
    INDIAN_BANK_CALENDAR,
    IST,
    FrozenClock,
    SettlementCalendar,
    SystemClock,
    day_bounds,
    ensure_aware,
    from_epoch,
    nth_weekday_of_month,
    parse_ist,
    to_epoch,
    to_ist,
)
from core.errors import ClockError, NaiveDatetime

dates = st.dates(min_value=dt.date(2025, 1, 1), max_value=dt.date(2027, 12, 31))
CAL = INDIAN_BANK_CALENDAR


# --------------------------------------------------------------------------- #
# Timezone discipline
# --------------------------------------------------------------------------- #
def test_ist_is_a_fixed_offset_with_no_dst() -> None:
    """India has not observed DST since 1945, so +05:30 is the definition, not an
    approximation - and using a fixed offset removes the tzdata dependency."""
    for month in range(1, 13):
        instant = dt.datetime(2026, month, 15, 12, 0, tzinfo=IST)
        assert instant.utcoffset() == dt.timedelta(hours=5, minutes=30)


def test_naive_datetimes_are_refused_at_the_boundary() -> None:
    with pytest.raises(NaiveDatetime):
        ensure_aware(dt.datetime(2026, 3, 31, 18, 30))
    with pytest.raises(NaiveDatetime):
        to_ist(dt.datetime(2026, 3, 31, 18, 30))


@pytest.mark.parametrize(
    "text",
    ["2026-03-31", "2026-03-31 18:30:00", "2026-03-31T18:30:00+05:30", "2026-03-31T13:00:00Z"],
)
def test_parse_ist_accepts_the_shapes_source_files_actually_use(text: str) -> None:
    parsed = parse_ist(text)
    assert parsed.tzinfo is not None
    assert to_ist(parsed).date() == dt.date(2026, 3, 31)


def test_unparseable_timestamp_is_a_typed_error() -> None:
    with pytest.raises(ClockError):
        parse_ist("31/03/2026")


@given(st.integers(min_value=0, max_value=4_102_444_800))
def test_epoch_round_trip(seconds: int) -> None:
    """Razorpay ships created_at as epoch seconds; the round trip must be lossless."""
    assert to_epoch(from_epoch(seconds)) == seconds


def test_day_bounds_are_half_open_and_cover_exactly_one_day() -> None:
    start, end = day_bounds(dt.date(2026, 3, 31))
    assert start.tzinfo is not None and end - start == dt.timedelta(days=1)
    assert start.hour == 0 and start.minute == 0


# --------------------------------------------------------------------------- #
# Clock injection
# --------------------------------------------------------------------------- #
def test_frozen_clock_does_not_move_on_its_own() -> None:
    clock = FrozenClock.at("2026-03-31 18:30")
    first = clock.now()
    assert clock.now() == first, "a frozen clock that ticks is not frozen"
    assert clock.today() == dt.date(2026, 3, 31)
    clock.advance(days=1)
    assert clock.today() == dt.date(2026, 4, 1)


def test_system_clock_is_aware() -> None:
    assert SystemClock().now().tzinfo is not None


def test_business_logic_never_calls_datetime_now() -> None:
    """Enforced by grep, because a single stray now() silently destroys replayability.

    core/clock.py is the one permitted home for the wall clock.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    offenders: list[str] = []
    for package in ("core", "recon", "forecast", "qa", "ingest"):
        for path in sorted((root / package).rglob("*.py")):
            if path.name == "clock.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr in {"now", "today", "utcnow"} and isinstance(
                    node.func.value, ast.Name | ast.Attribute
                ):
                    target = node.func.value
                    name = target.id if isinstance(target, ast.Name) else target.attr
                    if name in {"datetime", "date", "dt", "time"}:
                        offenders.append(
                            f"{path.relative_to(root).as_posix()}:{node.lineno} {name}.{node.func.attr}()"
                        )
    assert offenders == [], "wall-clock reads outside core/clock.py: " + "; ".join(offenders)


# --------------------------------------------------------------------------- #
# The Indian bank calendar
# --------------------------------------------------------------------------- #
def test_second_and_fourth_saturdays_are_closed_others_are_open() -> None:
    """The rule everyone forgets. March 2026 has Saturdays on 7/14/21/28."""
    assert CAL.is_business_day(dt.date(2026, 3, 7)) is True  # 1st Saturday
    assert CAL.is_business_day(dt.date(2026, 3, 14)) is False  # 2nd Saturday
    assert CAL.is_business_day(dt.date(2026, 3, 21)) is True  # 3rd Saturday
    assert CAL.is_business_day(dt.date(2026, 3, 28)) is False  # 4th Saturday


def test_sundays_and_gazetted_holidays_are_closed() -> None:
    assert not CAL.is_business_day(dt.date(2026, 3, 8))  # Sunday
    assert not CAL.is_business_day(dt.date(2026, 1, 26))  # Republic Day
    assert not CAL.is_business_day(dt.date(2026, 8, 15))  # Independence Day
    assert not CAL.is_business_day(dt.date(2026, 10, 2))  # Gandhi Jayanti


def test_every_closed_day_states_why() -> None:
    """Non-negotiable 1: every decision carries a human-readable reason."""
    assert CAL.why_closed(dt.date(2026, 3, 8)) == "Sunday"
    assert CAL.why_closed(dt.date(2026, 3, 14)) == "2nd Saturday (Indian banks closed)"
    assert CAL.why_closed(dt.date(2026, 3, 28)) == "4th Saturday (Indian banks closed)"
    assert CAL.why_closed(dt.date(2026, 1, 26)) == "bank holiday"
    assert CAL.why_closed(dt.date(2026, 3, 10)) is None


@given(dates)
def test_open_and_why_closed_always_agree(day: dt.date) -> None:
    assert CAL.is_business_day(day) == (CAL.why_closed(day) is None)


@given(dates)
def test_next_business_day_is_open_and_strictly_later(day: dt.date) -> None:
    nxt = CAL.next_business_day(day)
    assert nxt > day
    assert CAL.is_business_day(nxt)


@given(dates, st.integers(min_value=0, max_value=15))
def test_settlement_always_lands_on_an_open_day(day: dt.date, cycle: int) -> None:
    captured = dt.datetime.combine(day, dt.time(14, 0), tzinfo=IST)
    settles = CAL.settlement_date(captured, cycle_days=cycle)
    assert CAL.is_business_day(settles)
    assert settles >= day


@given(dates, st.integers(min_value=0, max_value=10))
def test_settlement_is_monotone_in_the_cycle_length(day: dt.date, cycle: int) -> None:
    """T+3 never lands before T+2 - obvious, and exactly the kind of off-by-one that
    a hand-rolled business-day loop gets wrong."""
    captured = dt.datetime.combine(day, dt.time(14, 0), tzinfo=IST)
    assert CAL.settlement_date(captured, cycle_days=cycle) <= CAL.settlement_date(
        captured, cycle_days=cycle + 1
    )


def test_the_long_weekend_case_that_creates_phantom_exceptions() -> None:
    """Thu 12 Mar and Fri 13 Mar 2026 both settle on Mon 16 Mar, because Sat 14 Mar
    is a 2nd Saturday and Sun 15 Mar is a Sunday. A naive +2 days would predict
    Sat 14 and Sun 15 and score both as breaks."""
    thursday = parse_ist("2026-03-12 14:00")
    friday = parse_ist("2026-03-13 14:00")
    assert CAL.settlement_date(thursday) == dt.date(2026, 3, 16)
    assert CAL.settlement_date(friday) == dt.date(2026, 3, 17)
    naive = (thursday + dt.timedelta(days=2)).date()
    assert naive == dt.date(2026, 3, 14) and not CAL.is_business_day(naive)


@given(dates)
def test_expected_window_contains_the_expected_date(day: dt.date) -> None:
    captured = dt.datetime.combine(day, dt.time(14, 0), tzinfo=IST)
    low, high = CAL.expected_window(captured)
    expected = CAL.settlement_date(captured)
    assert low <= expected <= high


@given(dates, st.integers(min_value=0, max_value=30))
def test_business_days_between_matches_a_brute_force_count(day: dt.date, span: int) -> None:
    end = day + dt.timedelta(days=span)
    brute = sum(
        1
        for k in range(1, span + 1)
        if CAL.is_business_day(day + dt.timedelta(days=k))
    )
    assert CAL.business_days_between(day, end) == brute


def test_business_days_between_is_antisymmetric() -> None:
    a, b = dt.date(2026, 3, 2), dt.date(2026, 3, 20)
    assert CAL.business_days_between(a, b) == -CAL.business_days_between(b, a)


def test_negative_cycles_are_refused() -> None:
    with pytest.raises(ClockError):
        CAL.settlement_date(parse_ist("2026-03-12 14:00"), cycle_days=-1)


def test_nth_weekday_of_month() -> None:
    assert nth_weekday_of_month(dt.date(2026, 3, 1)) == 1
    assert nth_weekday_of_month(dt.date(2026, 3, 8)) == 2
    assert nth_weekday_of_month(dt.date(2026, 3, 29)) == 5


def test_calendar_without_the_saturday_rule_still_closes_sundays() -> None:
    """Not every rail follows Indian bank hours; the rule is configurable."""
    plain = SettlementCalendar(holidays=frozenset(), saturday_rule=False)
    assert plain.is_business_day(dt.date(2026, 3, 14))
    assert not plain.is_business_day(dt.date(2026, 3, 15))


def test_calendar_is_immutable() -> None:
    with pytest.raises((AttributeError, TypeError)):
        CAL.saturday_rule = False  # type: ignore[misc]
