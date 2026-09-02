"""Time, injected - plus the settlement calendar that decides when money lands.

Two rules hold everywhere in Triveni:

1. **No business logic reads the wall clock.** Everything takes a :class:`Clock`.
   A reconciliation run for 2026-03-31 must produce identical output whether it is
   executed on the night of the 31st or replayed by a judge six months later, so
   ``datetime.now()`` never appears outside :class:`SystemClock`.
2. **No naive datetimes.** A timestamp without a zone is an ambiguity waiting to
   become a mismatch, and a T+2 window is exactly where it would bite.

IST is implemented as a fixed UTC+05:30 offset rather than ``ZoneInfo`` on purpose.
India has observed no daylight saving since 1945, so the offset is not an
approximation - it is the whole definition - and it removes a dependency on the
system tz database, which is absent on many Windows machines and would turn an
offline cold start into a download.

The bank calendar encodes the rule that catches people out: Indian banks close on
Sundays and on the **second and fourth Saturday** of each month, while the first,
third and fifth Saturdays are working days.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from core.errors import ClockError, NaiveDatetime

#: India Standard Time. Fixed offset, no DST, ever.
IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")
UTC: Final = dt.UTC

_CALENDAR_PATH: Final = Path(__file__).resolve().parent.parent / "data" / "calendars" / "holidays_in.json"


# --------------------------------------------------------------------------- #
# Clock
# --------------------------------------------------------------------------- #
@runtime_checkable
class Clock(Protocol):
    """Everything that needs the time takes one of these."""

    def now(self) -> dt.datetime:
        """Current instant, always timezone-aware."""
        ...

    def today(self) -> dt.date:
        """Current civil date in IST."""
        ...


@dataclass(frozen=True, slots=True)
class SystemClock:
    """The only place in Triveni that is allowed to read the wall clock."""

    tz: dt.tzinfo = IST

    def now(self) -> dt.datetime:
        return dt.datetime.now(tz=self.tz)

    def today(self) -> dt.date:
        return self.now().astimezone(IST).date()


@dataclass(slots=True)
class FrozenClock:
    """A clock that does not move unless told to. Every test uses this."""

    instant: dt.datetime

    def __post_init__(self) -> None:
        self.instant = ensure_aware(self.instant)

    def now(self) -> dt.datetime:
        return self.instant

    def today(self) -> dt.date:
        return self.instant.astimezone(IST).date()

    def advance(self, **delta: float) -> FrozenClock:
        """``clock.advance(days=1)`` - mutates and returns self for chaining."""
        self.instant = self.instant + dt.timedelta(**delta)
        return self

    @classmethod
    def at(cls, iso: str) -> FrozenClock:
        """``FrozenClock.at('2026-03-31 18:30')`` - parsed as IST if no zone given."""
        return cls(parse_ist(iso))


# --------------------------------------------------------------------------- #
# Conversions
# --------------------------------------------------------------------------- #
def ensure_aware(value: dt.datetime, *, assume: dt.tzinfo = IST) -> dt.datetime:
    """Reject naive datetimes loudly at the boundary, rather than guessing later."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise NaiveDatetime(
            "a naive datetime reached the ledger; attach a timezone at the boundary",
            value=value.isoformat(),
            hint=f"if this really is local time, call parse_ist() or replace(tzinfo={assume})",
        )
    return value


def to_ist(value: dt.datetime) -> dt.datetime:
    return ensure_aware(value).astimezone(IST)


def parse_ist(text: str) -> dt.datetime:
    """Parse an ISO-ish timestamp, defaulting a missing zone to IST.

    Source files arrive with ``2026-03-31``, ``2026-03-31 18:30:00`` and
    ``2026-03-31T18:30:00+05:30`` in the same column; all three land here.
    """
    raw = text.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ClockError("unparseable timestamp", value=text) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed


def from_epoch(seconds: int) -> dt.datetime:
    """Razorpay ships ``created_at`` as epoch seconds; this is the one way in."""
    return dt.datetime.fromtimestamp(seconds, tz=UTC).astimezone(IST)


def to_epoch(value: dt.datetime) -> int:
    return int(ensure_aware(value).timestamp())


def day_bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """The half-open IST interval ``[start, end)`` covering a civil date."""
    start = dt.datetime.combine(day, dt.time.min, tzinfo=IST)
    return start, start + dt.timedelta(days=1)


# --------------------------------------------------------------------------- #
# Settlement calendar
# --------------------------------------------------------------------------- #
def _ordinal(n: int) -> str:
    """1 -> '1st', 2 -> '2nd'. Reason strings are read by humans."""
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def nth_weekday_of_month(day: dt.date) -> int:
    """Which occurrence of its weekday this date is - 1 for the first Saturday, etc.

    This is what the 2nd/4th Saturday bank rule is written in terms of.
    """
    return (day.day - 1) // 7 + 1


@dataclass(frozen=True, slots=True)
class SettlementCalendar:
    """When Indian banks are open, and therefore when a payout can actually land.

    ``holidays`` is data, not code: it loads from
    ``data/calendars/holidays_in.json`` so a merchant in a different state can edit
    the list without touching the engine. Only the three gazetted **national**
    holidays are hard-coded as a floor, because every other bank holiday in India is
    state-specific and asserting a fuller list as fact would be inventing precision
    we do not have (see docs/limitations.md).
    """

    holidays: frozenset[dt.date] = field(default_factory=frozenset)
    saturday_rule: bool = True
    label: str = "IN-bank"

    @classmethod
    def indian_banks(cls, years: Iterable[int] = (2025, 2026, 2027)) -> SettlementCalendar:
        holidays: set[dt.date] = set()
        for year in sorted(years):
            holidays.update(
                {
                    dt.date(year, 1, 26),  # Republic Day  (gazetted, fixed)
                    dt.date(year, 8, 15),  # Independence Day (gazetted, fixed)
                    dt.date(year, 10, 2),  # Gandhi Jayanti (gazetted, fixed)
                    dt.date(year, 4, 1),  # RBI annual closing of accounts
                }
            )
        holidays.update(_load_extra_holidays())
        return cls(frozenset(holidays))

    def is_business_day(self, day: dt.date) -> bool:
        if day.weekday() == 6:  # Sunday
            return False
        if self.saturday_rule and day.weekday() == 5 and nth_weekday_of_month(day) in (2, 4):
            return False
        return day not in self.holidays

    def why_closed(self, day: dt.date) -> str | None:
        """Human-readable reason a day is not a settlement day, or ``None``.

        Every gate in Triveni carries a reason string; a calendar is no exception.
        """
        if day.weekday() == 6:
            return "Sunday"
        if self.saturday_rule and day.weekday() == 5 and nth_weekday_of_month(day) in (2, 4):
            return f"{_ordinal(nth_weekday_of_month(day))} Saturday (Indian banks closed)"
        if day in self.holidays:
            return "bank holiday"
        return None

    def next_business_day(self, day: dt.date) -> dt.date:
        cursor = day + dt.timedelta(days=1)
        for _ in range(400):
            if self.is_business_day(cursor):
                return cursor
            cursor += dt.timedelta(days=1)
        raise ClockError("no business day found within a year", start=day.isoformat())

    def add_business_days(self, day: dt.date, count: int) -> dt.date:
        """Advance ``count`` business days. ``count=0`` snaps forward to the next
        open day if ``day`` itself is closed."""
        if count < 0:
            raise ClockError("add_business_days does not go backwards", count=count)
        cursor = day
        if not self.is_business_day(cursor):
            cursor = self.next_business_day(cursor)
        for _ in range(count):
            cursor = self.next_business_day(cursor)
        return cursor

    def business_days_between(self, start: dt.date, end: dt.date) -> int:
        """Count of open days in ``(start, end]`` - the realised settlement lag."""
        if end < start:
            return -self.business_days_between(end, start)
        count, cursor = 0, start
        while cursor < end:
            cursor += dt.timedelta(days=1)
            if self.is_business_day(cursor):
                count += 1
        return count

    def settlement_date(self, captured_at: dt.datetime, *, cycle_days: int = 2) -> dt.date:
        """The date a payment captured at ``captured_at`` is expected to settle.

        T+2 is the default Razorpay cycle. The offset counts *business* days, which
        is precisely why a Thursday capture and a Friday capture can land on the same
        Monday - and why a naive date subtraction produces phantom `timing`
        exceptions all through a long weekend.
        """
        if cycle_days < 0:
            raise ClockError("settlement cycle cannot be negative", cycle_days=cycle_days)
        return self.add_business_days(to_ist(captured_at).date(), cycle_days)

    def expected_window(
        self, captured_at: dt.datetime, *, cycle_days: int = 2, slack_days: int = 1
    ) -> tuple[dt.date, dt.date]:
        """Inclusive date band in which the settlement may legitimately appear.

        Stage 1 matching uses this instead of an exact date equality, and
        ``slack_days`` is the tolerance that stops a one-day bank delay from being
        scored as a missing settlement.
        """
        expected = self.settlement_date(captured_at, cycle_days=cycle_days)
        early = expected
        for _ in range(slack_days):
            cursor = early - dt.timedelta(days=1)
            while not self.is_business_day(cursor):
                cursor -= dt.timedelta(days=1)
            early = cursor
        late = self.add_business_days(expected, slack_days)
        return early, late


def _load_extra_holidays() -> frozenset[dt.date]:
    """Optional, editable holiday list. Absent file is not an error."""
    if not _CALENDAR_PATH.exists():
        return frozenset()
    try:
        payload = json.loads(_CALENDAR_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ClockError("holiday calendar is present but unreadable", path=str(_CALENDAR_PATH)) from exc
    return frozenset(dt.date.fromisoformat(entry["date"]) for entry in payload.get("holidays", []))


#: The default calendar. Constructed once; it is immutable.
INDIAN_BANK_CALENDAR: Final = SettlementCalendar.indian_banks()
