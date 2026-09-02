"""The closed exception taxonomy, and the evidence bundle that travels with one.

Every row Triveni cannot confidently match becomes a typed exception. The type is
drawn from a **closed enum**, never a free string, for three reasons:

* free-text categories cannot be counted, so you can never say "38% of our breaks are
  timing" and mean it;
* a model that can invent a category will invent one rather than admit it does not
  know, and `unknown` is the honest answer we want it to reach for;
* the UI colour-codes and the triage inbox groups by type, and both need a fixed set.

Each exception carries an :class:`EvidenceBundle`: the candidate rows considered, the
per-field match weights that were computed, the arithmetic that was attempted, and the
reason the system stopped. That bundle is what a human sees when they open the row -
and what makes "abstain" a useful answer rather than a shrug.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.ids import IdKind, make_id
from core.money import Money, format_inr


class ExceptionType(StrEnum):
    """The closed set. Adding a member is a deliberate schema change, not a typo."""

    # --- timing ------------------------------------------------------------
    TIMING = "timing"
    """The money is there, on a different day. T+2 across a weekend or a holiday."""

    # --- deductions that are explainable arithmetic ------------------------
    FEE_MDR = "fee_mdr"
    """Merchant discount rate withheld by the gateway."""

    GST_ON_FEE = "gst_on_fee"
    """18% GST charged on the MDR - on the fee, not on the transaction."""

    TDS_194O = "tds_194o"
    """0.1% tax deducted at source under section 194-O."""

    FX = "fx"
    """Foreign-currency settlement converted at a rate we did not use."""

    # --- amount mismatches -------------------------------------------------
    SHORT_PAY = "short_pay"
    """Less arrived than was due, beyond any explainable deduction."""

    OVER_PAY = "over_pay"
    """More arrived than was due."""

    REFUND_OFFSET = "refund_offset"
    """A refund was netted against the same settlement cycle."""

    CHARGEBACK_HOLD = "chargeback_hold"
    """Funds withheld against a disputed transaction."""

    ROLLING_RESERVE = "rolling_reserve"
    """A percentage held back as security and released later."""

    # --- structural --------------------------------------------------------
    DUPLICATE = "duplicate"
    """The same payment appears twice in one source."""

    SPLIT_SETTLEMENT = "split_settlement"
    """One day's captures arrived as two or more separate credits."""

    MISSING_IN_BANK = "missing_in_bank"
    """The gateway says it settled; no bank credit can be found."""

    MISSING_IN_LEDGER = "missing_in_ledger"
    """Money arrived that the merchant's own books do not know about."""

    # --- the honest ones ---------------------------------------------------
    CORRUPT_ROW = "corrupt_row"
    """The source row could not be parsed. Never fatal; the batch continues."""

    UNKNOWN = "unknown"
    """Triveni could not classify this. Deliberately available, deliberately last."""


#: Types whose rupee difference is fully explainable by arithmetic. Everything here
#: should end up inside the settlement waterfall rather than in the triage queue.
EXPLAINABLE_TYPES: frozenset[ExceptionType] = frozenset(
    {
        ExceptionType.FEE_MDR,
        ExceptionType.GST_ON_FEE,
        ExceptionType.TDS_194O,
        ExceptionType.FX,
        ExceptionType.REFUND_OFFSET,
        ExceptionType.CHARGEBACK_HOLD,
        ExceptionType.ROLLING_RESERVE,
    }
)

#: Types that mean "a human must look at this", not "the system is broken".
TRIAGE_TYPES: frozenset[ExceptionType] = frozenset(
    {
        ExceptionType.SHORT_PAY,
        ExceptionType.OVER_PAY,
        ExceptionType.DUPLICATE,
        ExceptionType.MISSING_IN_BANK,
        ExceptionType.MISSING_IN_LEDGER,
        ExceptionType.CORRUPT_ROW,
        ExceptionType.UNKNOWN,
    }
)


class Severity(StrEnum):
    """How much attention this needs. Drives ordering in the triage inbox."""

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


def severity_for(exception_type: ExceptionType, amount: Money) -> Severity:
    """Severity from type and magnitude, deterministically.

    Explainable deductions are informational however large - a correctly computed
    ₹23,560 of MDR is not a problem. Missing money is high severity however small,
    because a missing row is a symptom rather than an amount.
    """
    if exception_type in {ExceptionType.MISSING_IN_BANK, ExceptionType.MISSING_IN_LEDGER}:
        return Severity.HIGH
    if exception_type in EXPLAINABLE_TYPES:
        return Severity.INFO
    magnitude = abs(amount).paise
    if magnitude >= 100_000_00:  # >= Rs 1,00,000
        return Severity.HIGH
    if magnitude >= 10_000_00:  # >= Rs 10,000
        return Severity.MEDIUM
    return Severity.LOW


class EvidenceItem(BaseModel):
    """One piece of support for - or against - a conclusion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1, description="candidate | field_weight | arithmetic | source_span")
    label: str
    detail: str = ""
    weight: str = ""
    source_id: str = ""
    span: tuple[int, int] | None = Field(
        default=None, description="character offsets into the source narration, for highlighting"
    )


class EvidenceBundle(BaseModel):
    """Everything the system looked at, so a human does not have to start over.

    This is the difference between an exception that helps and one that annoys: the
    candidates already considered, the weights already computed, the arithmetic
    already attempted, and the sentence explaining where it stopped.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    considered: tuple[str, ...] = ()
    """Ids of the candidate rows the blocker surfaced."""

    items: tuple[EvidenceItem, ...] = ()
    stage: str = ""
    """Which pipeline stage produced this - rules, blocking, fellegi_sunter, solver, llm."""

    llm_used: bool = False
    llm_confidence: str = ""
    abstained_because: str = ""

    def with_item(self, item: EvidenceItem) -> EvidenceBundle:
        return self.model_copy(update={"items": (*self.items, item)})

    def count(self) -> int:
        return len(self.items)


class ReconException(BaseModel):
    """A typed, evidenced, human-readable reason a row is not reconciled."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exception_id: str
    exception_type: ExceptionType
    severity: Severity
    reason: str = Field(min_length=1, description="human-readable, mandatory")
    amount: Money
    source_ids: tuple[str, ...]
    as_of: dt.date
    evidence: EvidenceBundle = Field(default_factory=EvidenceBundle)
    suggested_action: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_explainable(self) -> bool:
        return self.exception_type in EXPLAINABLE_TYPES

    def canonical(self) -> dict[str, Any]:
        return {
            "exception_id": self.exception_id,
            "type": self.exception_type.value,
            "severity": self.severity.value,
            "reason": self.reason,
            "amount": {"paise": self.amount.paise, "currency": self.amount.currency},
            "source_ids": list(self.source_ids),
            "as_of": self.as_of,
            "evidence": self.evidence.model_dump(),
            "suggested_action": self.suggested_action,
        }

    def render(self) -> str:
        return (
            f"[{self.severity.value:6}] {self.exception_type.value:18} "
            f"{format_inr(self.amount):>15}  {self.reason}"
        )


def make_exception(
    *,
    exception_type: ExceptionType,
    reason: str,
    amount: Money,
    source_ids: tuple[str, ...],
    as_of: dt.date,
    evidence: EvidenceBundle | None = None,
    suggested_action: str = "",
    metadata: dict[str, Any] | None = None,
) -> ReconException:
    """Build an exception with a content-addressed id, so re-running a close
    produces the same exception rather than a duplicate."""
    exception_id = make_id(
        IdKind.EXCEPTION, exception_type.value, sorted(source_ids), as_of, amount.paise
    )
    return ReconException(
        exception_id=exception_id,
        exception_type=exception_type,
        severity=severity_for(exception_type, amount),
        reason=reason,
        amount=amount,
        source_ids=tuple(sorted(source_ids)),
        as_of=as_of,
        evidence=evidence or EvidenceBundle(),
        suggested_action=suggested_action,
        metadata=metadata or {},
    )


#: Human-facing one-liners, used by the UI legend and the triage inbox.
DESCRIPTIONS: dict[ExceptionType, str] = {
    ExceptionType.TIMING: "The money is there, on a different day.",
    ExceptionType.FEE_MDR: "The gateway withheld its merchant discount rate.",
    ExceptionType.GST_ON_FEE: "18% GST was charged on the fee, not on the sale.",
    ExceptionType.TDS_194O: "0.1% was deducted at source under section 194-O.",
    ExceptionType.FX: "Settled in a foreign currency at a rate we did not book.",
    ExceptionType.SHORT_PAY: "Less arrived than was due, beyond any explainable deduction.",
    ExceptionType.OVER_PAY: "More arrived than was due.",
    ExceptionType.REFUND_OFFSET: "A refund was netted against this settlement cycle.",
    ExceptionType.CHARGEBACK_HOLD: "Funds are held against a dispute.",
    ExceptionType.ROLLING_RESERVE: "A percentage is held back as security, released later.",
    ExceptionType.DUPLICATE: "The same payment appears twice in one source.",
    ExceptionType.SPLIT_SETTLEMENT: "One day's captures arrived as several credits.",
    ExceptionType.MISSING_IN_BANK: "The gateway settled it; the bank has no matching credit.",
    ExceptionType.MISSING_IN_LEDGER: "Money arrived that the books do not know about.",
    ExceptionType.CORRUPT_ROW: "The source row could not be parsed.",
    ExceptionType.UNKNOWN: "Triveni could not classify this and is not guessing.",
}
