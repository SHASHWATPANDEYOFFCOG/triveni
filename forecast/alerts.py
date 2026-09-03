"""The liquidity alert: fire on the lower bound, not the point forecast.

Everyone alerts on the point forecast. It is the obvious thing and it is wrong in a
specific, expensive way.

A point forecast is a *median*. Alerting when it falls below a committed outflow means
alerting when there is a 50% chance of a shortfall - which is far too late, because by
then the coin has already been flipped. Worse, it stays silent in exactly the situation
a treasurer most needs to hear about: a forecast of Rs 6,00,000 against a Rs 5,00,000
payroll looks comfortable, and if the band runs from Rs 2,00,000 to Rs 10,00,000 it is
not comfortable at all, it is a coin toss about whether salaries clear.

So Triveni alerts on the **lower bound of the conformal interval**. The rule is: warn
when the amount we can be confident of receiving falls short of what is already
committed. With a 90% band, that means "there is roughly a one-in-twenty chance of
being short by at least this much" - a statement a treasurer can act on, and one that
fires early enough to act.

**Triveni proposes; it never moves money.** Every recommendation here is a sentence for
a human - "delay this payout by one day", "draw Rs X on the overdraft" - written to the
audit log and rendered in the UI. There is no code path from an alert to a transfer,
because there is no transfer capability anywhere in the system to reach.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from core.money import Money, format_inr
from forecast.backtest import Interval


class Severity(StrEnum):
    INFO = "info"
    WATCH = "watch"
    """The lower bound is close to the commitment. Nothing to do yet."""

    WARN = "warn"
    """The lower bound falls short. There is a real chance of being unable to pay."""

    CRITICAL = "critical"
    """Even the point forecast falls short. This is not a risk, it is a plan."""


class CommitmentKind(StrEnum):
    PAYROLL = "payroll"
    GST = "gst"
    TDS = "tds"
    EMI = "emi"
    VENDOR = "vendor"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class Commitment:
    """Money that must go out on a known date. The thing a forecast is measured against."""

    due_on: dt.date
    amount: Money
    kind: CommitmentKind
    label: str

    def canonical(self) -> dict[str, object]:
        return {
            "due_on": self.due_on,
            "amount_paise": self.amount.paise,
            "kind": self.kind.value,
            "label": self.label,
        }


@dataclass(frozen=True, slots=True)
class LiquidityAlert:
    """A warning a treasurer can act on, with the arithmetic that produced it."""

    due_on: dt.date
    commitment: Commitment
    expected: Money
    """The point forecast of cash available by the due date."""

    confident: Money
    """The lower bound - what we can be confident of. The number the alert uses."""

    shortfall: Money
    """How far the *confident* figure falls below the commitment."""

    severity: Severity
    reason: str
    proposal: str
    nominal_coverage: Decimal

    def canonical(self) -> dict[str, object]:
        return {
            "due_on": self.due_on,
            "commitment": self.commitment.canonical(),
            "expected_paise": self.expected.paise,
            "confident_paise": self.confident.paise,
            "shortfall_paise": self.shortfall.paise,
            "severity": self.severity.value,
            "reason": self.reason,
            "proposal": self.proposal,
            "nominal_coverage": self.nominal_coverage,
        }

    def render(self) -> str:
        return (
            f"  [{self.severity.value:8}] {self.due_on}  {self.commitment.label}\n"
            f"             {self.reason}\n"
            f"             proposal: {self.proposal}"
        )


def cumulative_by(
    intervals: Sequence[Interval], due_on: dt.date, *, opening_balance: Money | None = None
) -> tuple[Money, Money]:
    """Cash expected and cash we can be confident of, by a date.

    Cumulative because a commitment on the 20th is met by everything that lands up to
    the 20th, not by the 20th's receipts alone. Summing the lower bounds is
    deliberately conservative - the true joint lower bound of a sum is tighter than the
    sum of marginal lower bounds, and erring toward caution is the right direction for
    a solvency warning.
    """
    opening = opening_balance or Money.zero()
    expected = opening.paise
    confident = opening.paise
    for interval in intervals:
        if interval.date > due_on:
            break
        expected += interval.point
        confident += interval.lower
    return Money(expected), Money(confident)


def check(
    intervals: Sequence[Interval],
    commitments: Sequence[Commitment],
    *,
    opening_balance: Money | None = None,
    nominal_coverage: Decimal = Decimal("0.90"),
    watch_margin: Decimal = Decimal("0.15"),
) -> list[LiquidityAlert]:
    """Raise an alert wherever confident cash falls short of a commitment.

    ``watch_margin`` produces the quieter `watch` level when the lower bound clears
    the commitment but only just - a 15% cushion on a payroll is not comfort, and a
    treasurer would rather know on Tuesday than on Friday.
    """
    alerts: list[LiquidityAlert] = []

    for commitment in sorted(commitments, key=lambda c: (c.due_on, c.label)):
        expected, confident = cumulative_by(
            intervals, commitment.due_on, opening_balance=opening_balance
        )
        due = commitment.amount
        shortfall = Money(max(due.paise - confident.paise, 0))
        cushion = confident.paise - due.paise

        if expected.paise < due.paise:
            severity = Severity.CRITICAL
            reason = (
                f"even the central forecast of {format_inr(expected)} falls short of "
                f"the {format_inr(due)} due; this is not a risk, it is a plan"
            )
        elif shortfall.paise > 0:
            severity = Severity.WARN
            reason = (
                f"we can be confident of {format_inr(confident)} by {commitment.due_on} "
                f"against {format_inr(due)} due - short by {format_inr(shortfall)}. The "
                f"central forecast of {format_inr(expected)} looks comfortable, which is "
                f"exactly why alerting on it would have stayed silent"
            )
        elif cushion < int(Decimal(due.paise) * watch_margin):
            severity = Severity.WATCH
            reason = (
                f"the confident figure of {format_inr(confident)} clears the "
                f"{format_inr(due)} due, but by under "
                f"{watch_margin:.0%} - worth knowing now rather than on the day"
            )
        else:
            continue

        alerts.append(
            LiquidityAlert(
                due_on=commitment.due_on,
                commitment=commitment,
                expected=expected,
                confident=confident,
                shortfall=shortfall,
                severity=severity,
                reason=reason,
                nominal_coverage=nominal_coverage,
                proposal=_propose(commitment, shortfall, severity),
            )
        )

    return alerts


def _propose(commitment: Commitment, shortfall: Money, severity: Severity) -> str:
    """A sentence for a human. Triveni never acts on any of these itself."""
    if severity is Severity.WATCH:
        return (
            f"No action needed yet. Re-check the morning of {commitment.due_on}; the "
            f"band will have narrowed by then."
        )
    if commitment.kind in (CommitmentKind.GST, CommitmentKind.TDS):
        return (
            f"Statutory and cannot be delayed. Arrange {format_inr(shortfall)} of "
            f"headroom before {commitment.due_on}, or bring a settlement forward."
        )
    if commitment.kind is CommitmentKind.VENDOR:
        return (
            f"Consider delaying {format_inr(shortfall)} of vendor payment by one "
            f"business day, or part-paying. Triveni proposes; a human decides."
        )
    return (
        f"Arrange {format_inr(shortfall)} of headroom before {commitment.due_on} - "
        f"an overdraft draw or a delayed discretionary payout. Triveni proposes; a "
        f"human decides."
    )


def default_commitments(
    start: dt.date, horizon_days: int, monthly_payroll: Money
) -> list[Commitment]:
    """The outflows an Indian SMB has whether or not anyone modelled them.

    Illustrative amounts derived from the payroll figure rather than invented
    independently, so a merchant substituting their own payroll moves the whole set
    coherently.
    """
    out: list[Commitment] = []
    end = start + dt.timedelta(days=horizon_days)
    cursor = start
    seen: set[tuple[int, int]] = set()

    while cursor <= end:
        month_key = (cursor.year, cursor.month)
        if month_key not in seen:
            seen.add(month_key)
            last_day = (
                dt.date(cursor.year + (cursor.month == 12), (cursor.month % 12) + 1, 1)
                - dt.timedelta(days=1)
            )
            for due, amount, kind, label in (
                (last_day, monthly_payroll, CommitmentKind.PAYROLL, "monthly payroll"),
                (
                    dt.date(cursor.year, cursor.month, 20),
                    Money(monthly_payroll.paise // 5),
                    CommitmentKind.GST,
                    "GST return",
                ),
                (
                    dt.date(cursor.year, cursor.month, 7),
                    Money(monthly_payroll.paise // 20),
                    CommitmentKind.TDS,
                    "TDS deposit",
                ),
            ):
                if start <= due <= end:
                    out.append(Commitment(due_on=due, amount=amount, kind=kind, label=label))
        cursor += dt.timedelta(days=1)

    return sorted(out, key=lambda c: (c.due_on, c.label))
