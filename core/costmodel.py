"""What being wrong costs, in rupees.

Accuracy metrics alone cannot tell you where to set a threshold, because the two ways
of being wrong are not equally bad. Triveni's errors have asymmetric consequences:

* A **false match** silently corrupts the books. The row looks reconciled, so nobody
  looks at it again. It surfaces weeks later during a close or an audit, when the
  trail is cold, and someone spends an hour unwinding it - or it never surfaces and
  becomes a write-off. This is the expensive error.
* A **false non-match** is a row that should have matched and did not. It lands in
  the triage queue and a human clears it in a couple of minutes. Annoying, cheap.
* An **abstention** is a false non-match that Triveni was honest about. Same human
  minutes, no pretence of correctness - which is why abstaining is a modelled option
  with a price rather than a failure.

Because the costs differ by roughly two orders of magnitude, the accuracy-optimal
threshold and the cost-optimal threshold are not the same number. That gap is what
the alpha slider in the UI lets a judge see and move.

**On the numbers below.** Every parameter is an :class:`Assumption` carrying its own
provenance string, and the defaults are labelled ``illustrative`` because we have not
sourced them from a specific published study. They are stated, overridable and shown
in the UI rather than buried, so a merchant can substitute their own and the whole
model moves with them. Rule A.6: an assumption that announces itself is honest; an
assumption dressed as a finding is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from core.money import Money, Rate, format_inr


@dataclass(frozen=True, slots=True)
class Assumption:
    """A number we chose, with the reason we chose it attached.

    ``source`` is either a citation or the literal word ``illustrative``. There is no
    third option, and nothing in Triveni may use a bare number that skipped this type.
    """

    value: Decimal
    unit: str
    label: str
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, Decimal):
            raise TypeError(f"{self.label}: assumptions are Decimal, never float")
        if not self.source.strip():
            raise ValueError(f"{self.label}: an assumption must state its source")

    @property
    def is_illustrative(self) -> bool:
        return self.source.strip().lower().startswith("illustrative")

    def describe(self) -> str:
        marker = " [illustrative assumption]" if self.is_illustrative else ""
        return f"{self.label} = {self.value} {self.unit}{marker}"

    def canonical(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "unit": self.unit,
            "label": self.label,
            "source": self.source,
            "illustrative": self.is_illustrative,
        }


_ILLUSTRATIVE = (
    "illustrative - stated, not sourced; override with your own figures in "
    "core/costmodel.py or via the UI"
)


@dataclass(frozen=True, slots=True)
class CostModel:
    """Converts an outcome mix into rupees.

    All arithmetic goes through :class:`~core.money.Money` and :class:`~core.money.Rate`,
    so a cost is exact to the paise like every other amount in the system.
    """

    analyst_cost_per_hour: Assumption = field(
        default_factory=lambda: Assumption(
            Decimal("600"),
            "INR/hour",
            "loaded cost of a finance analyst",
            _ILLUSTRATIVE,
        )
    )
    minutes_to_triage_exception: Assumption = field(
        default_factory=lambda: Assumption(
            Decimal("3"),
            "minutes",
            "time to clear one queued exception with evidence in front of you",
            _ILLUSTRATIVE,
        )
    )
    minutes_to_unwind_false_match: Assumption = field(
        default_factory=lambda: Assumption(
            Decimal("45"),
            "minutes",
            "time to find and reverse a wrong match discovered weeks later",
            _ILLUSTRATIVE,
        )
    )
    minutes_to_review_flagged: Assumption = field(
        default_factory=lambda: Assumption(
            Decimal("2"),
            "minutes",
            "time to confirm a match Triveni already proposed with evidence",
            _ILLUSTRATIVE,
        )
    )
    writeoff_share_of_false_match: Assumption = field(
        default_factory=lambda: Assumption(
            Decimal("0.05"),
            "fraction",
            "share of wrongly matched value never recovered",
            _ILLUSTRATIVE,
        )
    )
    manual_minutes_per_row: Assumption = field(
        default_factory=lambda: Assumption(
            Decimal("0.5"),
            "minutes",
            "time to eyeball one row in a spreadsheet when reconciling by hand",
            _ILLUSTRATIVE,
        )
    )

    # --- primitives --------------------------------------------------------
    def cost_of_minutes(self, minutes: Decimal) -> Money:
        """Human time, priced. Rounded once, at the paise."""
        rupees = (minutes * self.analyst_cost_per_hour.value / Decimal(60)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        return Money.from_rupees(rupees)

    def cost_per_false_match(self, exposed: Money) -> Money:
        """Unwinding time, plus the share of the exposed value never recovered."""
        labour = self.cost_of_minutes(self.minutes_to_unwind_false_match.value)
        writeoff = Rate(
            self.writeoff_share_of_false_match.value, label="write-off share"
        ).of(abs(exposed))
        return labour + writeoff

    def cost_per_false_non_match(self) -> Money:
        return self.cost_of_minutes(self.minutes_to_triage_exception.value)

    def cost_per_review(self) -> Money:
        return self.cost_of_minutes(self.minutes_to_review_flagged.value)

    def manual_baseline(self, rows: int, exceptions: int = 0) -> Money:
        """What the same day costs with no Triveni at all: a person and a spreadsheet.

        Structurally this has to be two terms, not one. Someone reconciling by hand
        skims every row *and* spends materially longer on the ones that do not tie
        out. Charging only the skim would understate the manual cost and flatter
        Triveni; charging triage on every row would overstate it and flatter it
        differently. "We saved X minutes" only means something against a baseline
        that describes the old way accurately.
        """
        minutes = self.manual_minutes_per_row.value * Decimal(rows) + (
            self.minutes_to_triage_exception.value * Decimal(exceptions)
        )
        return self.cost_of_minutes(minutes)

    def break_even_false_matches(
        self, *, rows: int, exceptions: int, reviewed: int, missed: int, avg_exposure: Money
    ) -> Decimal:
        """How many false matches Triveni may make before automation stops paying.

        This is the number that actually governs where the conformal threshold
        belongs. Auto-posting is only worth doing while the cost of the mistakes it
        makes stays below the cost of the human labour it removes - and because a
        false match costs roughly an order of magnitude more than a missed one, that
        budget is small. Returns the count; compare it against the realised count.
        """
        budget = self.manual_baseline(rows, exceptions) - (
            self.cost_per_false_non_match() * missed + self.cost_per_review() * reviewed
        )
        if budget.paise <= 0:
            return Decimal(0)
        per_false_match = self.cost_of_minutes(
            self.minutes_to_unwind_false_match.value
        ) + Rate(self.writeoff_share_of_false_match.value).of(abs(avg_exposure))
        if per_false_match.paise <= 0:
            return Decimal("Infinity")
        return (Decimal(budget.paise) / Decimal(per_false_match.paise)).quantize(Decimal("0.01"))

    def with_analyst_cost(self, rupees_per_hour: str | Decimal) -> CostModel:
        return replace(
            self,
            analyst_cost_per_hour=replace(
                self.analyst_cost_per_hour, value=Decimal(rupees_per_hour)
            ),
        )

    def assumptions(self) -> tuple[Assumption, ...]:
        return (
            self.analyst_cost_per_hour,
            self.minutes_to_triage_exception,
            self.minutes_to_unwind_false_match,
            self.minutes_to_review_flagged,
            self.writeoff_share_of_false_match,
            self.manual_minutes_per_row,
        )

    def canonical(self) -> dict[str, Any]:
        return {a.label: a.canonical() for a in self.assumptions()}


@dataclass(frozen=True, slots=True)
class OutcomeMix:
    """What one reconciliation run actually produced."""

    rows: int
    auto_posted: int = 0
    true_matches: int = 0
    false_matches: int = 0
    false_non_matches: int = 0
    abstained: int = 0
    needs_review: int = 0
    exposed_by_false_matches: Money = field(default_factory=Money.zero)

    @property
    def human_touched(self) -> int:
        return self.abstained + self.needs_review + self.false_non_matches


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """The rupee consequence of a run, itemised so it can be argued with."""

    false_match_cost: Money
    false_non_match_cost: Money
    review_cost: Money
    total: Money
    manual_baseline: Money
    human_minutes: Decimal

    @property
    def saved_vs_manual(self) -> Money:
        return self.manual_baseline - self.total

    def render(self) -> str:
        lines = [
            f"  false matches        {format_inr(self.false_match_cost):>16}",
            f"  missed matches       {format_inr(self.false_non_match_cost):>16}",
            f"  human review         {format_inr(self.review_cost):>16}",
            f"  {'-' * 37}",
            f"  cost of this run     {format_inr(self.total):>16}",
            f"  manual baseline      {format_inr(self.manual_baseline):>16}",
            f"  saved                {format_inr(self.saved_vs_manual):>16}",
            f"  human minutes        {self.human_minutes:>16}",
        ]
        return "\n".join(lines)

    def canonical(self) -> dict[str, Any]:
        return {
            "false_match_cost_paise": self.false_match_cost.paise,
            "false_non_match_cost_paise": self.false_non_match_cost.paise,
            "review_cost_paise": self.review_cost.paise,
            "total_paise": self.total.paise,
            "manual_baseline_paise": self.manual_baseline.paise,
            "saved_paise": self.saved_vs_manual.paise,
            "human_minutes": self.human_minutes,
        }


def price(mix: OutcomeMix, model: CostModel | None = None) -> CostBreakdown:
    """Price one run's outcome mix.

    Note what is *not* charged: a correct auto-post costs nothing, because nobody
    looked at it. That is the whole economic case, and it is also why the false-match
    cost has to be modelled honestly - if auto-posting were free of downside, the
    optimal policy would be to auto-post everything.
    """
    model = model or CostModel()

    false_match_cost = (
        model.cost_per_false_match(mix.exposed_by_false_matches)
        if mix.false_matches
        else Money.zero()
    )
    if mix.false_matches > 1:
        # The unwinding labour is per incident; the write-off share is already
        # computed over the total exposed value, so only the labour multiplies.
        labour = model.cost_of_minutes(model.minutes_to_unwind_false_match.value)
        false_match_cost = false_match_cost + labour * (mix.false_matches - 1)

    false_non_match_cost = model.cost_per_false_non_match() * mix.false_non_matches
    review_cost = model.cost_per_review() * (mix.abstained + mix.needs_review)

    human_minutes = (
        model.minutes_to_triage_exception.value * Decimal(mix.false_non_matches)
        + model.minutes_to_review_flagged.value * Decimal(mix.abstained + mix.needs_review)
        + model.minutes_to_unwind_false_match.value * Decimal(mix.false_matches)
    )

    return CostBreakdown(
        false_match_cost=false_match_cost,
        false_non_match_cost=false_non_match_cost,
        review_cost=review_cost,
        total=false_match_cost + false_non_match_cost + review_cost,
        manual_baseline=model.manual_baseline(mix.rows, mix.human_touched),
        human_minutes=human_minutes,
    )
