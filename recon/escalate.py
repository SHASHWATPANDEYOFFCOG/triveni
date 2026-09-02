"""Stage 6: the residue. The only place a model touches reconciliation.

Everything the deterministic keys, the blocker, Fellegi-Sunter and the global solver
could not resolve arrives here. The model's job is narrow and stated:

* it **classifies** a row into the closed exception taxonomy, and
* it **explains** in one sentence, quoting only text that was put in front of it.

It does not pick a match. It does not compute or adjust an amount. It does not set a
threshold. Those live in `recon/assign.py`, `recon/waterfall.py` and
`core/conformal.py`, and a test greps this module's siblings to prove the separation
holds rather than merely being intended.

**Three ways to abstain, all of them first-class.**

1. The gateway abstained - injection denied, budget spent, schema violated, mode off.
2. The samples disagreed. The prompt is run *k* times and agreement is measured on the
   *decision*, not the token string, in the spirit of semantic entropy (Farquhar et
   al., Nature 2024): a model that says "timing difference" and "settlement timing"
   has agreed with itself about what to do, while one that says `timing` and
   `short_pay` has not, however fluent both were.
3. The model said `unknown`, which the prompt explicitly tells it is a correct answer
   and preferred to a confident wrong one.

In every case the row becomes a typed exception carrying the evidence and the reason
it abstained - which is more useful to a human than a guess, and is the difference
between a system that says "I don't know" and one that cannot.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from core.llm import LLMGateway, LLMOutcome
from core.money import format_inr
from ingest.canonical import CanonicalTxn
from recon.exceptions import (
    EvidenceBundle,
    EvidenceItem,
    ExceptionType,
    ReconException,
    make_exception,
)

#: Below this agreement across k samples, the row abstains however confident any
#: single sample was. Two of three agreeing is not a decision, it is a coin toss with
#: extra steps.
MIN_AGREEMENT: Decimal = Decimal("0.67")

#: Below this self-reported confidence, abstain. Self-reported confidence is weak
#: evidence on its own, which is why it is only ever used *alongside* agreement.
MIN_CONFIDENCE: Decimal = Decimal("0.55")

#: How many times to sample. Odd, so a majority exists.
SAMPLES: int = 3


@dataclass(frozen=True, slots=True)
class Escalation:
    """One residue row, and what became of it."""

    row_id: str
    exception: ReconException
    outcome: LLMOutcome | None
    used_model: bool

    @property
    def abstained(self) -> bool:
        return self.exception.exception_type is ExceptionType.UNKNOWN


@dataclass(slots=True)
class EscalationReport:
    escalations: list[Escalation] = field(default_factory=list)
    considered: int = 0
    called: int = 0
    abstained: int = 0
    denied: int = 0

    @property
    def call_rate(self) -> Decimal:
        return Decimal(self.called) / Decimal(self.considered) if self.considered else Decimal(0)

    def render(self) -> str:
        return (
            f"{self.considered} residue row(s); {self.called} sent to a model "
            f"({self.call_rate:.1%}); {self.abstained} abstained; {self.denied} denied"
        )


def _describe(row: CanonicalTxn) -> str:
    """What the model is shown. Structured, minimal, and free of anything it could
    mistake for an instruction from us."""
    return (
        f"source: {row.source.value}\n"
        f"kind: {row.kind.value}\n"
        f"id: {row.external_id}\n"
        f"amount: {format_inr(row.amount)}\n"
        f"date: {row.occurred_on}\n"
        f"reference: {row.reference or '(none)'}\n"
        f"counterparty: {row.counterparty or '(none)'}"
    )


def _describe_candidates(candidates: Sequence[CanonicalTxn]) -> str:
    if not candidates:
        return "(the matcher found no plausible candidate)"
    return "\n".join(
        f"- {c.source.value} {c.external_id} {format_inr(c.amount)} on {c.occurred_on}"
        for c in candidates[:6]
    )


def escalate(
    rows: Sequence[CanonicalTxn],
    candidates: dict[str, Sequence[CanonicalTxn]],
    *,
    gateway: LLMGateway,
    as_of: dt.date,
    max_rows: int = 50,
) -> EscalationReport:
    """Classify and explain the residue. Never matches, always explains.

    ``max_rows`` is a cost bound, not a quality one: the rows beyond it still become
    typed exceptions, they simply get the deterministic reason rather than a model's.
    An unbounded model loop over a bad batch is how an agent runs up a bill.
    """
    report = EscalationReport(considered=len(rows))

    for index, row in enumerate(sorted(rows, key=lambda r: r.txn_id)):
        row_candidates = candidates.get(row.txn_id, ())

        if index >= max_rows:
            report.escalations.append(
                Escalation(
                    row_id=row.txn_id,
                    exception=_unknown(
                        row,
                        as_of,
                        "beyond this run's model-call budget; classified without a model",
                        row_candidates,
                    ),
                    outcome=None,
                    used_model=False,
                )
            )
            continue

        report.called += 1
        outcome = gateway.call(
            "classify_residue",
            {"candidates": _describe_candidates(row_candidates)},
            untrusted={"row": _describe(row) + f"\nnarration: {row.raw_narration}"},
            samples=SAMPLES,
            decision_key="exception_type",
        )

        if outcome.abstained:
            if "injection" in outcome.reason:
                report.denied += 1
            report.abstained += 1
            report.escalations.append(
                Escalation(
                    row_id=row.txn_id,
                    exception=_unknown(row, as_of, outcome.reason, row_candidates),
                    outcome=outcome,
                    used_model=True,
                )
            )
            continue

        confidence = Decimal(str(outcome.data.get("confidence", 0)))
        classified = str(outcome.data.get("exception_type", "unknown"))

        if outcome.agreement < MIN_AGREEMENT:
            reason = (
                f"the model gave different answers across {SAMPLES} samples "
                f"(agreement {outcome.agreement:.0%}); abstaining rather than "
                f"picking one of them"
            )
            report.abstained += 1
            report.escalations.append(
                Escalation(row.txn_id, _unknown(row, as_of, reason, row_candidates), outcome, True)
            )
            continue

        if confidence < MIN_CONFIDENCE or classified == ExceptionType.UNKNOWN.value:
            reason = (
                str(outcome.data.get("reason", "")).strip()
                or f"the model reported confidence {confidence} and did not classify"
            )
            report.abstained += 1
            report.escalations.append(
                Escalation(row.txn_id, _unknown(row, as_of, reason, row_candidates), outcome, True)
            )
            continue

        report.escalations.append(
            Escalation(
                row_id=row.txn_id,
                exception=make_exception(
                    exception_type=ExceptionType(classified),
                    reason=str(outcome.data.get("reason", "")).strip()
                    or f"classified as {classified}",
                    amount=row.amount,
                    source_ids=(row.txn_id,),
                    as_of=as_of,
                    evidence=_evidence(row, row_candidates, outcome),
                    suggested_action=str(outcome.data.get("suggested_action", "")).strip(),
                    metadata={"llm_agreement": str(outcome.agreement), "model": outcome.model},
                ),
                outcome=outcome,
                used_model=True,
            )
        )

    return report


def _evidence(
    row: CanonicalTxn, candidates: Sequence[CanonicalTxn], outcome: LLMOutcome | None
) -> EvidenceBundle:
    items = [
        EvidenceItem(
            kind="source_span",
            label="narration",
            detail=row.raw_narration or "(none)",
            source_id=row.txn_id,
        ),
        EvidenceItem(
            kind="arithmetic",
            label="amount",
            detail=format_inr(row.amount),
            source_id=row.txn_id,
        ),
    ]
    items.extend(
        EvidenceItem(
            kind="candidate",
            label=f"{c.source.value} {c.external_id}",
            detail=f"{format_inr(c.amount)} on {c.occurred_on}",
            source_id=c.txn_id,
        )
        for c in candidates[:6]
    )
    return EvidenceBundle(
        considered=tuple(sorted(c.txn_id for c in candidates)),
        items=tuple(items),
        stage="stage6.escalate",
        llm_used=outcome is not None,
        llm_confidence=str(outcome.data.get("confidence", "")) if outcome else "",
        abstained_because="" if outcome and outcome.ok else (outcome.reason if outcome else ""),
    )


def _unknown(
    row: CanonicalTxn,
    as_of: dt.date,
    reason: str,
    candidates: Sequence[CanonicalTxn],
) -> ReconException:
    """The honest outcome. Typed, evidenced, and explicit about why it stopped."""
    bundle = _evidence(row, candidates, None)
    return make_exception(
        exception_type=ExceptionType.UNKNOWN,
        reason=reason or "could not be classified",
        amount=row.amount,
        source_ids=(row.txn_id,),
        as_of=as_of,
        evidence=EvidenceBundle(
            considered=bundle.considered,
            items=bundle.items,
            stage="stage6.escalate",
            llm_used=False,
            abstained_because=reason,
        ),
        suggested_action=(
            "Open the evidence bundle: the candidates the matcher rejected are listed "
            "with their amounts and dates."
        ),
    )
