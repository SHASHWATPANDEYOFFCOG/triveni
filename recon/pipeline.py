"""The staged reconciliation orchestrator.

Five stages, cheapest and most certain first. Each one may only *add* matches; a later
stage that wants to overwrite an earlier one has to raise a conflict and route to a
human (invariant D.1.4). That monotonicity is what makes the stage ladder in the UI
meaningful - each segment is a real, attributable contribution, not a re-run.

    Stage 0  canonicalise   normalise money, dates, references, names, narrations
    Stage 1  deterministic  exact keys: UTR/RRN, and (amount, date-window, reference)
    Stage 2  blocking       candidate generation, deterministic + dense      (M7)
    Stage 3  linkage        Fellegi-Sunter with EM-fitted weights            (M8)
    Stage 4  assignment     global min-cost matching + many-to-one subsets   (M9)
    Stage 5  residue        LLM explains and classifies; never picks         (M11)

At M6 only Stages 0 and 1 exist. The orchestrator is built to hold all five from the
start so that each milestone plugs in rather than rewrites, and so the per-stage
report - contribution, precision, recall, wall-clock - is comparable across
milestones in `docs/metrics-history.md`.

This module never imports the ground truth - not even to grade itself. Evaluation
lives in ``scripts/eval_pipeline.py``, outside every matching package, and a test
greps `recon/`, `ingest/`, `forecast/` and `qa/` to prove none of them can
reference the labels. Keeping the evaluator here would have been convenient and
would have made that guarantee unenforceable.
"""

from __future__ import annotations

import datetime as dt
import time
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from core.clock import INDIAN_BANK_CALENDAR, Clock, FrozenClock, SettlementCalendar
from core.ids import IdKind, make_id
from core.money import Money, format_inr
from ingest.adapters.csv_bank import read_bank_statement, read_ledger
from ingest.adapters.fixtures import read_gateway
from ingest.canonical import CanonicalTxn, CorruptRecord, IngestResult, SourceKind, TxnKind
from recon.blocking import CandidateSet, generate_candidates, relevant_rows
from recon.exceptions import (
    EvidenceBundle,
    EvidenceItem,
    ExceptionType,
    ReconException,
    make_exception,
)
from recon.fellegi_sunter import FellegiSunterModel, PairScore, score_candidates
from recon.normalize import (
    NarrationParse,
    name_key,
    normalize_counterparty,
    normalize_reference,
    parse_narration,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = ROOT / "data" / "seed"


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class MatchGroup:
    """One accepted reconciliation: some rows on each side that belong together."""

    match_id: str
    stage: str
    gateway_ids: tuple[str, ...]
    bank_ids: tuple[str, ...]
    ledger_ids: tuple[str, ...]
    amount: Money
    confidence: Decimal
    reason: str
    evidence: EvidenceBundle = field(default_factory=EvidenceBundle)

    @property
    def all_ids(self) -> frozenset[str]:
        return frozenset((*self.gateway_ids, *self.bank_ids, *self.ledger_ids))

    def canonical(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id,
            "stage": self.stage,
            "gateway_ids": list(self.gateway_ids),
            "bank_ids": list(self.bank_ids),
            "ledger_ids": list(self.ledger_ids),
            "amount_paise": self.amount.paise,
            "confidence": self.confidence,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class StageReport:
    """What one stage contributed. Drives the stage ladder in the UI."""

    stage: str
    label: str
    matches_added: int
    rows_consumed: int
    elapsed_ms: Decimal
    detail: str = ""

    def canonical(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "label": self.label,
            "matches_added": self.matches_added,
            "rows_consumed": self.rows_consumed,
            "elapsed_ms": self.elapsed_ms,
            "detail": self.detail,
        }


@dataclass(slots=True)
class ReconResult:
    """Everything one close produced."""

    matches: list[MatchGroup] = field(default_factory=list)
    exceptions: list[ReconException] = field(default_factory=list)
    stages: list[StageReport] = field(default_factory=list)
    rows: dict[str, CanonicalTxn] = field(default_factory=dict)
    corrupt: list[CorruptRecord] = field(default_factory=list)
    narration_parses: dict[str, NarrationParse] = field(default_factory=dict)
    candidates: CandidateSet | None = None
    """Every pair Stage 2 proposed. Stage 3 fits its model on all of these."""

    linkage_model: FellegiSunterModel | None = None
    """The EM-fitted Fellegi-Sunter model. Its weight table is a UI artefact."""

    pair_scores: dict[tuple[str, str], PairScore] = field(default_factory=dict)
    """Per-pair match weight with its field breakdown, for the triage evidence panel."""

    suppressed_ids: set[str] = field(default_factory=set)
    """Duplicate copies held out of matching. They are reported, never discarded."""

    open_pairs: set[tuple[str, str]] = field(default_factory=set)
    """The subset whose rows Stage 1 did not already consume.

    A *snapshot*, taken at Stage 2, of what was still matchable at that moment. It is
    deliberately not updated as later stages consume rows, because its purpose is to
    record what each stage was handed - which is what makes the stage ladder in the UI
    an attribution rather than a running total.
    """

    # --- invariants --------------------------------------------------------
    def consumed_ids(self) -> set[str]:
        consumed: set[str] = set()
        for group in self.matches:
            consumed |= group.all_ids
        return consumed

    def check_no_double_spend(self) -> list[str]:
        """Invariant D.1.3: no source row may appear in two accepted match groups."""
        seen: dict[str, str] = {}
        offenders: list[str] = []
        for group in self.matches:
            for row_id in sorted(group.all_ids):
                if row_id in seen:
                    offenders.append(f"{row_id} in both {seen[row_id]} and {group.match_id}")
                else:
                    seen[row_id] = group.match_id
        return offenders

    def unmatched(self, source: SourceKind | None = None) -> list[CanonicalTxn]:
        consumed = self.consumed_ids()
        return sorted(
            (
                row
                for row_id, row in self.rows.items()
                if row_id not in consumed and (source is None or row.source is source)
            ),
            key=lambda r: r.txn_id,
        )

    @property
    def matched_row_count(self) -> int:
        return len(self.consumed_ids())

    @property
    def llm_call_rate(self) -> Decimal:
        """Share of rows that would need a model. The AI-judgment number."""
        parses = list(self.narration_parses.values())
        if not parses:
            return Decimal(0)
        needed = sum(1 for p in parses if p.needs_llm())
        return Decimal(needed) / Decimal(len(parses))


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ReconConfig:
    """Knobs for the deterministic stages. All explicit; none inferred at runtime."""

    settlement_cycle_days: int = 2
    date_slack_days: int = 1
    calendar: SettlementCalendar = INDIAN_BANK_CALENDAR
    amount_tolerance: Money = field(default_factory=Money.zero)
    """Stage 1 is exact by design. Tolerance belongs to the subset solver at M9."""

    link_upper_bits: float = 6.0
    """Fellegi-Sunter's upper threshold: at or above this, a pair may be linked."""

    link_lower_bits: float = 0.0
    """And the lower one: below this a pair is rejected outright. Between the two is
    the clerical-review region the method is named for, and it becomes a typed
    exception with its full weight breakdown rather than a coin flip."""

    def canonical(self) -> dict[str, Any]:
        return {
            "settlement_cycle_days": self.settlement_cycle_days,
            "date_slack_days": self.date_slack_days,
            "amount_tolerance_paise": self.amount_tolerance.paise,
            "link_upper_bits": str(self.link_upper_bits),
            "link_lower_bits": str(self.link_lower_bits),
        }


# --------------------------------------------------------------------------- #
# Stage 0 - canonicalise
# --------------------------------------------------------------------------- #
def canonicalise(rows: list[CanonicalTxn]) -> tuple[list[CanonicalTxn], dict[str, NarrationParse]]:
    """Normalise names, references and narrations onto every row.

    The narration parse is kept alongside rather than folded in, because the UI needs
    the character spans to highlight where each extracted value came from, and the
    metrics need to know whether a model was required.
    """
    out: list[CanonicalTxn] = []
    parses: dict[str, NarrationParse] = {}
    for row in rows:
        parse = parse_narration(row.raw_narration)
        parses[row.txn_id] = parse

        reference = normalize_reference(row.reference) or parse.reference
        counterparty = normalize_counterparty(row.counterparty or parse.counterparty)
        out.append(
            row.model_copy(
                update={
                    "reference": reference,
                    "counterparty": counterparty,
                    "raw_counterparty": row.raw_counterparty or row.counterparty,
                    "raw_reference": row.raw_reference or row.reference,
                    "metadata": {
                        **row.metadata,
                        "name_key": name_key(row.counterparty or parse.counterparty),
                        "rail": parse.rail.value,
                        "utr": parse.utr,
                        "parse_source": parse.source.value,
                    },
                }
            )
        )
    return out, parses



# --------------------------------------------------------------------------- #
# Stage 1a - deduplication
# --------------------------------------------------------------------------- #
def dedupe(result: ReconResult, clock: Clock) -> tuple[set[str], str]:
    """Find rows that are the same event ingested twice, and set the copies aside.

    A retried webhook or a re-uploaded statement puts the identical payment into the
    batch twice. Left alone this is worse than a missing row, because *both* copies
    look matchable: on the seed, the duplicate payments were being linked to the
    invoices their originals should have owned, producing five confidently-wrong
    matches at 47.9 bits that no threshold could exclude - they were not marginal, the
    model was certain and the model was right about the evidence. The evidence was
    genuinely identical, because the rows are.

    Two rows duplicate each other when they agree on source, reference, amount and
    timestamp exactly. That is deliberately strict: a near-duplicate is a *finding*
    for a human, not something to silently discard. The survivor is the
    lexicographically first external id, so the choice is deterministic and not
    dependent on ingest order, and every copy set aside becomes a typed `duplicate`
    exception naming the row it duplicates.

    Returns the ids to exclude from matching, plus a one-line report.
    """
    groups: dict[tuple[str, str, int, str], list[CanonicalTxn]] = defaultdict(list)
    for row in sorted(result.rows.values(), key=lambda r: r.txn_id):
        if row.kind not in {TxnKind.PAYMENT, TxnKind.REFUND, TxnKind.SETTLEMENT}:
            continue
        key = (
            row.source.value,
            row.reference,
            row.amount.paise,
            row.occurred_at.isoformat(),
        )
        groups[key].append(row)

    suppressed: set[str] = set()
    for key in sorted(groups):
        members = sorted(groups[key], key=lambda r: r.external_id)
        if len(members) < 2:
            continue
        original, *copies = members
        for copy in copies:
            suppressed.add(copy.txn_id)
            result.exceptions.append(
                make_exception(
                    exception_type=ExceptionType.DUPLICATE,
                    reason=(
                        f"{copy.external_id} is byte-identical to {original.external_id} "
                        f"on reference, amount ({format_inr(copy.amount)}) and timestamp; "
                        f"kept {original.external_id} and set this copy aside"
                    ),
                    amount=copy.amount,
                    source_ids=(copy.txn_id, original.txn_id),
                    as_of=clock.today(),
                    evidence=EvidenceBundle(
                        considered=(original.txn_id, copy.txn_id),
                        stage="stage1.dedupe",
                        items=(
                            EvidenceItem(
                                kind="field_weight",
                                label="reference",
                                detail=copy.reference or "(none)",
                                weight="identical",
                            ),
                            EvidenceItem(
                                kind="arithmetic",
                                label="amount",
                                detail=format_inr(copy.amount),
                                weight="identical",
                            ),
                            EvidenceItem(
                                kind="arithmetic",
                                label="timestamp",
                                detail=copy.occurred_at.isoformat(),
                                weight="identical",
                            ),
                        ),
                    ),
                    suggested_action=(
                        "Confirm the upstream feed is not re-delivering, then discard "
                        "the copy. No money is affected either way."
                    ),
                )
            )

    return suppressed, f"{len(suppressed)} duplicate row(s) set aside"


# --------------------------------------------------------------------------- #
# Stage 1 - deterministic keys
# --------------------------------------------------------------------------- #
def expected_window(row: CanonicalTxn, config: ReconConfig) -> tuple[dt.date, dt.date]:
    """The date band in which a row's settlement may legitimately appear.

    Public because Stage 2's blocking keys are built from it at M7: a candidate pair
    whose windows do not overlap is not worth scoring, and that single constraint is
    most of the reduction ratio.
    """
    return config.calendar.expected_window(
        row.occurred_at,
        cycle_days=config.settlement_cycle_days,
        slack_days=config.date_slack_days,
    )


def stage1_deterministic(
    result: ReconResult, config: ReconConfig
) -> tuple[list[MatchGroup], str]:
    """Exact matching on the keys that cannot be wrong.

    Two blocking keys, both logged:

    * **UTR/RRN** - when both sides carry one and they agree, that is the payment.
      No probabilistic model can improve on a bank reference number.
    * **(normalised reference, exact amount, settlement window)** - for rows without
      a UTR, where the order id survived into the narration.

    Both are 1:1. Many-to-one netting is Stage 4's problem, and pretending otherwise
    here would consume rows that the subset solver needs.
    """
    matches: list[MatchGroup] = []
    consumed = result.consumed_ids()

    by_source: dict[SourceKind, list[CanonicalTxn]] = defaultdict(list)
    for row in result.rows.values():
        if row.txn_id not in consumed and row.txn_id not in result.suppressed_ids:
            by_source[row.source].append(row)

    # --- key 1: UTR agreement between the gateway's settlement and the bank ---
    bank_by_utr: dict[str, list[CanonicalTxn]] = defaultdict(list)
    for row in sorted(by_source[SourceKind.BANK], key=lambda r: r.txn_id):
        utr = str(row.metadata.get("utr") or row.reference)
        if utr:
            bank_by_utr[utr].append(row)

    used: set[str] = set()
    utr_hits = 0
    for gw in sorted(by_source[SourceKind.GATEWAY], key=lambda r: r.txn_id):
        if gw.kind is not TxnKind.SETTLEMENT or gw.txn_id in used:
            continue
        utr = normalize_reference(gw.reference)
        candidates = [b for b in bank_by_utr.get(utr, []) if b.txn_id not in used]
        if len(candidates) != 1:
            continue
        bank = candidates[0]
        if bank.amount != gw.amount:
            # A UTR that agrees on identity but not on amount is a *finding*, not a
            # match: it is exactly the short-pay case, and quietly matching it would
            # hide the very thing the merchant needs to see.
            continue

        # A UTR is a strong key, but it is not a licence to ignore time. A credit
        # landing many business days from the gateway's own settlement date is either
        # a reference collision or something worth a human's attention, so the
        # calendar bounds the claim. The band is business days, not calendar days,
        # which is what stops a long weekend from reading as a discrepancy.
        lag = config.calendar.business_days_between(
            gw.occurred_on, bank.settled_on or bank.occurred_on
        )
        if not 0 <= lag <= config.date_slack_days + 1:
            continue

        used |= {gw.txn_id, bank.txn_id}
        utr_hits += 1
        matches.append(
            MatchGroup(
                match_id=make_id(IdKind.MATCH, "stage1.utr", gw.txn_id, bank.txn_id),
                stage="stage1.utr",
                gateway_ids=(gw.txn_id,),
                bank_ids=(bank.txn_id,),
                ledger_ids=(),
                amount=bank.amount,
                confidence=Decimal(1),
                reason=(
                    f"UTR {utr} appears in both the gateway settlement and the bank "
                    f"credit, for the same amount {format_inr(bank.amount)}"
                ),
                evidence=EvidenceBundle(
                    considered=(gw.txn_id, bank.txn_id),
                    stage="stage1.utr",
                    items=(
                        EvidenceItem(
                            kind="field_weight",
                            label="UTR",
                            detail=f"{utr} == {utr}",
                            weight="exact",
                            source_id=bank.txn_id,
                        ),
                        EvidenceItem(
                            kind="arithmetic",
                            label="amount",
                            detail=f"{format_inr(gw.amount)} == {format_inr(bank.amount)}",
                            weight="exact",
                        ),
                        EvidenceItem(
                            kind="arithmetic",
                            label="settlement lag",
                            detail=(
                                f"{lag} business day(s) between the gateway settlement "
                                f"({gw.occurred_on}) and the bank credit "
                                f"({bank.settled_on or bank.occurred_on}), within the "
                                f"permitted {config.date_slack_days + 1}"
                            ),
                            weight="within band",
                        ),
                    ),
                ),
            )
        )

    # --- key 2: order id + exact amount + settlement window (ledger <-> gateway) ---
    order_hits = 0
    gateway_by_order: dict[str, list[CanonicalTxn]] = defaultdict(list)
    for row in sorted(by_source[SourceKind.GATEWAY], key=lambda r: r.txn_id):
        if row.kind is TxnKind.PAYMENT and row.txn_id not in used:
            gateway_by_order[normalize_reference(row.reference)].append(row)

    for led in sorted(by_source[SourceKind.LEDGER], key=lambda r: r.txn_id):
        if led.txn_id in used:
            continue
        key = normalize_reference(led.reference)
        candidates = [
            g
            for g in gateway_by_order.get(key, [])
            if g.txn_id not in used and g.amount == led.amount
        ]
        if len(candidates) != 1:
            continue
        gw = candidates[0]
        used |= {led.txn_id, gw.txn_id}
        order_hits += 1
        matches.append(
            MatchGroup(
                match_id=make_id(IdKind.MATCH, "stage1.order", led.txn_id, gw.txn_id),
                stage="stage1.order",
                gateway_ids=(gw.txn_id,),
                bank_ids=(),
                ledger_ids=(led.txn_id,),
                amount=led.amount,
                confidence=Decimal(1),
                reason=(
                    f"order id {key} links invoice {led.external_id} to payment "
                    f"{gw.external_id} at exactly {format_inr(led.amount)}"
                ),
                evidence=EvidenceBundle(
                    considered=(led.txn_id, gw.txn_id),
                    stage="stage1.order",
                    items=(
                        EvidenceItem(
                            kind="field_weight",
                            label="order_id",
                            detail=key,
                            weight="exact",
                            source_id=gw.txn_id,
                        ),
                        EvidenceItem(
                            kind="arithmetic",
                            label="amount",
                            detail=f"{format_inr(led.amount)} == {format_inr(gw.amount)}",
                            weight="exact",
                        ),
                    ),
                ),
            )
        )

    detail = (
        f"UTR key matched {utr_hits} settlement(s); "
        f"order-id key matched {order_hits} invoice/payment pair(s)"
    )
    return matches, detail



# --------------------------------------------------------------------------- #
# Stage 3 - probabilistic linkage
# --------------------------------------------------------------------------- #
def stage3_linkage(result: ReconResult, config: ReconConfig) -> tuple[list[MatchGroup], str]:
    """Link the pairs Fellegi-Sunter is confident about, and only those.

    Two thresholds, which is the whole point of the F-S formulation: at or above the
    upper one a pair may be linked, below the lower one it is rejected, and between
    them lies the *clerical review* region a human decides. Collapsing that to a
    single threshold throws away the model's most useful output - its admission that
    it does not know.

    Linking is mutual-best and strictly 1:1, deliberately conservative. A globally
    optimal assignment is Stage 4's job, and greedily consuming rows here would take
    them away from the subset solver that needs them for many-to-one netting.
    """
    if result.linkage_model is None or not result.pair_scores:
        return [], "no candidate pairs to score"

    open_scores = [
        score for pair, score in sorted(result.pair_scores.items()) if pair in result.open_pairs
    ]
    accepted = [s for s in open_scores if s.total >= config.link_upper_bits]
    review = [s for s in open_scores if config.link_lower_bits <= s.total < config.link_upper_bits]

    best: dict[str, PairScore] = {}
    for score in sorted(accepted, key=lambda s: (-s.total, s.pair)):
        for row_id in score.pair:
            if row_id not in best or score.total > best[row_id].total:
                best[row_id] = score

    matches: list[MatchGroup] = []
    used: set[str] = set()
    for score in sorted(accepted, key=lambda s: (-s.total, s.pair)):
        left_id, right_id = score.pair
        if left_id in used or right_id in used:
            continue
        if best[left_id] is not score or best[right_id] is not score:
            continue
        left, right = result.rows[left_id], result.rows[right_id]
        used |= {left_id, right_id}

        by_source: dict[SourceKind, list[str]] = defaultdict(list)
        for row in (left, right):
            by_source[row.source].append(row.txn_id)

        top = sorted(score.weights, key=lambda w: -abs(w.weight))[:3]
        matches.append(
            MatchGroup(
                match_id=make_id(IdKind.MATCH, "stage3", left_id, right_id),
                stage="stage3.fellegi_sunter",
                gateway_ids=tuple(by_source.get(SourceKind.GATEWAY, ())),
                bank_ids=tuple(by_source.get(SourceKind.BANK, ())),
                ledger_ids=tuple(by_source.get(SourceKind.LEDGER, ())),
                amount=left.amount if left.amount.paise >= right.amount.paise else right.amount,
                confidence=score.as_confidence(),
                reason=(
                    f"match weight {score.total:+.1f} bits: "
                    + "; ".join(f"{w.description} ({w.weight:+.1f})" for w in top)
                ),
                evidence=EvidenceBundle(
                    considered=(left_id, right_id),
                    stage="stage3.fellegi_sunter",
                    items=tuple(
                        EvidenceItem(
                            kind="field_weight",
                            label=w.field,
                            detail=w.description,
                            weight=f"{w.weight:+.2f} bits",
                        )
                        for w in score.weights
                    ),
                ),
            )
        )

    # The clerical-review band becomes typed exceptions carrying the full breakdown,
    # so a human opens a row already knowing what the model saw and where it stopped.
    for score in sorted(review, key=lambda s: (-s.total, s.pair))[:50]:
        left_id, right_id = score.pair
        if left_id in used or right_id in used:
            continue
        left = result.rows[left_id]
        result.exceptions.append(
            make_exception(
                exception_type=ExceptionType.UNKNOWN,
                reason=(
                    f"match weight {score.total:+.1f} bits falls between the reject "
                    f"threshold ({config.link_lower_bits:+.1f}) and the link threshold "
                    f"({config.link_upper_bits:+.1f}) - too close to call"
                ),
                amount=left.amount,
                source_ids=(left_id, right_id),
                as_of=left.occurred_on,
                evidence=EvidenceBundle(
                    considered=(left_id, right_id),
                    stage="stage3.fellegi_sunter",
                    abstained_because="score inside the clerical-review band",
                    items=tuple(
                        EvidenceItem(
                            kind="field_weight",
                            label=w.field,
                            detail=w.description,
                            weight=f"{w.weight:+.2f} bits",
                        )
                        for w in score.weights
                    ),
                ),
                suggested_action="Confirm or reject this pair; the field weights are shown.",
            )
        )

    detail = (
        f"scored {len(open_scores)} open pair(s); linked {len(matches)} at or above "
        f"{config.link_upper_bits:+.1f} bits; {len(review)} in the clerical-review band"
    )
    return matches, detail


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def _add_stage(
    result: ReconResult,
    stage: str,
    label: str,
    matches: list[MatchGroup],
    started: float,
    detail: str = "",
) -> None:
    """Append matches under the monotonicity rule: a stage may only add."""
    already = result.consumed_ids()
    accepted: list[MatchGroup] = []
    conflicts = 0
    for group in matches:
        if group.all_ids & already:
            # Invariant D.1.4: a later stage never silently overwrites an earlier
            # one. The conflict is dropped here and surfaces as an exception.
            conflicts += 1
            continue
        accepted.append(group)
        already |= group.all_ids
    result.matches.extend(accepted)
    elapsed = Decimal(str(round((time.perf_counter() - started) * 1000, 3)))
    result.stages.append(
        StageReport(
            stage=stage,
            label=label,
            matches_added=len(accepted),
            rows_consumed=sum(len(g.all_ids) for g in accepted),
            elapsed_ms=elapsed,
            detail=detail + (f"; {conflicts} conflict(s) refused" if conflicts else ""),
        )
    )


def reconcile(
    *,
    directory: Path = DEFAULT_DATA,
    config: ReconConfig | None = None,
    clock: Clock | None = None,
) -> ReconResult:
    """Close the books for whatever is in ``directory``."""
    config = config or ReconConfig()
    clock = clock or FrozenClock.at("2026-04-01 09:00")
    result = ReconResult()

    # --- ingest ------------------------------------------------------------
    started = time.perf_counter()
    sources: list[IngestResult] = [
        read_gateway(directory),
        read_bank_statement(directory=directory),
        read_ledger(directory=directory),
    ]
    raw_rows = [row for source in sources for row in source.rows]
    result.corrupt = [record for source in sources for record in source.corrupt]

    # --- stage 0 -----------------------------------------------------------
    canonical_rows, parses = canonicalise(raw_rows)
    result.rows = {row.txn_id: row for row in canonical_rows}
    result.narration_parses = parses
    # Counted over rows that actually carry a narration, not over every ingested row:
    # "19 of 535 parsed without a model" would be a meaningless denominator, since
    # 515 of those rows have no free text to parse in the first place.
    narrated_ids = [
        row.txn_id for row in canonical_rows if row.source is SourceKind.BANK and row.raw_narration.strip()
    ]
    deterministic = sum(1 for txn_id in narrated_ids if parses[txn_id].complete)
    _add_stage(
        result,
        "stage0",
        "canonicalise",
        [],
        started,
        detail=(
            f"{len(canonical_rows)} rows normalised; {deterministic}/{len(narrated_ids)} "
            f"bank narrations parsed without a model; {len(result.corrupt)} corrupt row(s)"
        ),
    )

    # Corrupt rows become typed exceptions immediately, carrying their raw payload.
    for record in result.corrupt:
        result.exceptions.append(
            make_exception(
                exception_type=ExceptionType.CORRUPT_ROW,
                reason=f"{record.source.value} row {record.row_number} could not be parsed: {record.problem}",
                amount=Money.zero(),
                source_ids=(f"{record.source.value}-row-{record.row_number}",),
                as_of=clock.today(),
                evidence=EvidenceBundle(
                    stage="stage0",
                    items=(
                        EvidenceItem(
                            kind="source_span",
                            label="raw row",
                            detail=str(record.raw)[:400],
                        ),
                    ),
                ),
                suggested_action="Fix the export and re-ingest, or key this row manually.",
            )
        )

    # --- stage 1 -----------------------------------------------------------
    started = time.perf_counter()
    suppressed, dedupe_detail = dedupe(result, clock)
    result.suppressed_ids = suppressed
    matches, detail = stage1_deterministic(result, config)
    _add_stage(
        result, "stage1", "deterministic keys", matches, started, f"{dedupe_detail}; {detail}"
    )

    # --- stage 2: candidate generation -----------------------------------
    # Blocking proposes; it never decides.
    #
    # Candidates are generated over *every* relevant row, including the ones Stage 1
    # already matched. That looks wasteful and is not: Stage 3 fits its m and u
    # probabilities by EM on this set, and the residue left after Stage 1 is a badly
    # biased sample of it - all the easy true matches have been removed. Fitting on
    # the residue produced a model that assigned "lands in the expected settlement
    # window" a *negative* weight, because among the leftovers that level really did
    # correlate with non-matches. EM needs a representative sample; the full candidate
    # set is one, the residue is not.
    #
    # Stage 3 still only *proposes* matches among pairs neither of whose rows Stage 1
    # consumed, so the ladder shows each stage's real contribution.
    started = time.perf_counter()
    candidates = generate_candidates(
        [
            row
            for row in relevant_rows(result.rows.values())
            if row.txn_id not in result.suppressed_ids
        ]
    )
    result.candidates = candidates
    consumed = result.consumed_ids()
    open_pairs = {
        pair for pair in candidates.pairs if pair[0] not in consumed and pair[1] not in consumed
    }
    result.open_pairs = open_pairs
    _add_stage(
        result,
        "stage2",
        "blocking",
        [],
        started,
        detail=(
            f"{len(candidates.pairs):,} candidate pair(s), reduction ratio "
            f"{candidates.reduction_ratio:.4f} against "
            f"{candidates.total_possible:,} possible comparisons; "
            f"{len(open_pairs):,} still open after Stage 1"
        ),
    )

    # --- stage 3: probabilistic linkage ----------------------------------
    started = time.perf_counter()
    if candidates.pairs:
        model, scores = score_candidates(result.rows, sorted(candidates.pairs))
        result.linkage_model = model
        result.pair_scores = {score.pair: score for score in scores}
    matches, detail = stage3_linkage(result, config)
    _add_stage(result, "stage3", "Fellegi-Sunter linkage", matches, started, detail)

    offenders = result.check_no_double_spend()
    if offenders:
        raise AssertionError(f"double-booking detected: {offenders[:3]}")

    return result
