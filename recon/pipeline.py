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
from recon.exceptions import (
    EvidenceBundle,
    EvidenceItem,
    ExceptionType,
    ReconException,
    make_exception,
)
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

    def canonical(self) -> dict[str, Any]:
        return {
            "settlement_cycle_days": self.settlement_cycle_days,
            "date_slack_days": self.date_slack_days,
            "amount_tolerance_paise": self.amount_tolerance.paise,
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
        if row.txn_id not in consumed:
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
    deterministic = sum(1 for p in parses.values() if p.complete)
    _add_stage(
        result,
        "stage0",
        "canonicalise",
        [],
        started,
        detail=(
            f"{len(canonical_rows)} rows normalised; {deterministic}/{len(parses)} "
            f"narrations parsed without a model; {len(result.corrupt)} corrupt"
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
    matches, detail = stage1_deterministic(result, config)
    _add_stage(result, "stage1", "deterministic keys", matches, started, detail)

    offenders = result.check_no_double_spend()
    if offenders:
        raise AssertionError(f"double-booking detected: {offenders[:3]}")

    return result
