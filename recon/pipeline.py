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
from core.errors import InfeasibleAssignment
from core.ids import IdKind, make_id
from core.money import Money, format_inr
from ingest.adapters.csv_bank import read_bank_statement, read_ledger
from ingest.adapters.fixtures import read_gateway
from ingest.canonical import (
    CanonicalTxn,
    CorruptRecord,
    Direction,
    IngestResult,
    SourceKind,
    TxnKind,
)
from recon.assign import (
    Bucket,
    ManyToOneResult,
    assign_many_to_one,
    check_no_double_use,
)
from recon.blocking import CandidateSet, generate_candidates, relevant_rows
from recon.exceptions import (
    EvidenceBundle,
    EvidenceItem,
    ExceptionType,
    ReconException,
    make_exception,
)
from recon.fees import DEFAULT_RATE_CARD, RateCard
from recon.fellegi_sunter import FellegiSunterModel, PairScore, score_candidates
from recon.normalize import (
    NarrationParse,
    name_key,
    normalize_counterparty,
    normalize_reference,
    parse_narration,
)
from recon.subsetsum import Tolerance

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

    links: tuple[tuple[str, str], ...] = ()
    """Explicit 1:1 (gateway row, ledger row) pairings this group asserts.

    Recorded rather than inferred from co-membership, because the two are entirely
    different claims and conflating them is quietly catastrophic. A settlement group
    holding 21 payments and 19 invoices does *not* assert that every payment matches
    every invoice - that would be 399 claims where there are 19. Reading it that way
    dropped measured precision from 1.000 to 0.0996 without a single matching
    decision having changed.

    The relations a group asserts are therefore: each payment to the bank credit
    (many-to-one, from group membership), each invoice to the credit likewise, and
    payment-to-invoice *only* where a link is listed here.
    """

    @property
    def all_ids(self) -> frozenset[str]:
        return frozenset((*self.gateway_ids, *self.bank_ids, *self.ledger_ids))

    def relation_claims(self) -> set[tuple[str, str]]:
        """``(relation, row_id)`` for everything this group consumes.

        A row may appear once per relation and no more. `pays_invoice` is keyed on the
        ledger row because an invoice may be settled by exactly one payment;
        `settles_into` on the gateway or ledger row because each belongs to exactly
        one bank credit.
        """
        claims: set[tuple[str, str]] = set()
        for _gateway_id, ledger_id in self.links:
            claims.add(("pays_invoice", ledger_id))
        if self.bank_ids:
            for row_id in (*self.gateway_ids, *self.ledger_ids):
                claims.add(("settles_into", row_id))
        return claims

    def claimed_pairs(self) -> set[tuple[str, str]]:
        """Every pairwise claim this group makes, and no more.

        Mirrors `scripts/truth.py::true_pairs_from_group` exactly. If the two ever
        diverge, precision and recall stop meaning anything, so they are written to
        be read side by side.
        """
        pairs: set[tuple[str, str]] = set()

        def add(a: str, b: str) -> None:
            pairs.add((a, b) if a < b else (b, a))

        for left, right in self.links:
            add(left, right)
        for bank_id in self.bank_ids:
            for gateway_id in self.gateway_ids:
                add(gateway_id, bank_id)
            for ledger_id in self.ledger_ids:
                add(ledger_id, bank_id)
        return pairs

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
            "links": [list(link) for link in self.links],
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

    netting: ManyToOneResult | None = None
    """Stage 4's global attribution of payments to netted settlements."""

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
        """Invariant D.1.3, stated per *relation* rather than per row.

        The literal reading - "every source row appears in at most one match group" -
        is wrong for a three-way reconciliation with netting, and following it
        literally did real damage. A payment genuinely participates in two different
        relations: it pays an invoice, and it settles into a bank credit. Forcing both
        into a single group meant Stage 4 absorbed Stage 1's certain invoice links
        into an uncertain settlement attribution, dragging their confidence from 1.00
        down to 0.85 and taking the auto-postable share of the whole batch to zero.
        Nothing about those invoice links had become less certain.

        The substantive guarantee - the one that stops money being counted twice - is
        per relation: an invoice may be paid by at most one payment, a payment may
        settle into at most one credit, a credit may be claimed by at most one
        settlement record. That is what is enforced here, and it is strictly stronger
        than the row-level rule in the cases that matter while permitting the
        decomposition the domain actually has.
        """
        claimed: dict[tuple[str, str], str] = {}
        offenders: list[str] = []
        for group in sorted(self.matches, key=lambda g: g.match_id):
            for relation, row_id in group.relation_claims():
                key = (relation, row_id)
                if key in claimed:
                    offenders.append(
                        f"{row_id} claimed twice for '{relation}' by "
                        f"{claimed[key]} and {group.match_id}"
                    )
                else:
                    claimed[key] = group.match_id
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

    rate_card: RateCard = DEFAULT_RATE_CARD
    """What the merchant is charged. Lets Stage 4 aim at each payment's expected NET
    rather than its gross, which collapses the solver's feasible region from a 6%
    band to rounding noise. M10 fits these rates from the batch and reports where the
    card and reality disagree."""

    solver_budget: float = 0.5
    """CP-SAT budget in *deterministic* work units, not seconds.

    Two reasons for both halves of that. A wall-clock limit made the answer depend on
    machine speed, and two runs of the same reconciliation returned different
    attributions - fatal for a project whose metrics are meant to be byte-identical.

    And the budget is deliberately *small*. Measured on the seed, precision falls as
    the budget rises: 0.9169 at 0.5 units, 0.8329 at 2.0. The date-based hint is a very
    good solution, and given more time the solver walks away from it toward answers
    with a better objective value and worse actual accuracy. That is not a bug in
    CP-SAT, it is a reminder that the objective is a proxy for the truth and not the
    truth - so buying more optimisation of an imperfect proxy is not free."""

    attribution_weight: int = 3
    """How much attributing a payment is worth, relative to the unexplained money it
    creates. See recon/assign.py - this is the precision/recall dial for Stage 4, and
    the default was chosen by measured rupee cost, not by feel."""

    max_deduction_share: str = "0.06"
    """Largest plausible total deduction as a share of gross, for the subset tolerance.

    6% covers the worst case in the Indian rate card: a 4.3% international-card MDR,
    18% GST on that fee, and 0.1% TDS. M10 replaces this bound with rates fitted from
    the batch, at which point the band narrows to the fitting residual.
    """

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
            "max_deduction_share": self.max_deduction_share,
            "attribution_weight": self.attribution_weight,
            "solver_budget": str(self.solver_budget),
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
                links=((gw.txn_id, led.txn_id),),
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
                links=tuple(
                    (gateway_id, ledger_id)
                    for gateway_id in by_source.get(SourceKind.GATEWAY, ())
                    for ledger_id in by_source.get(SourceKind.LEDGER, ())
                ),
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
# Stage 4 - global assignment and many-to-one netting
# --------------------------------------------------------------------------- #
def stage4_global_assignment(
    result: ReconResult, config: ReconConfig
) -> tuple[list[MatchGroup], str]:
    """Attribute the day's payments to the netted credits, all at once.

    Each unmatched bank credit becomes a *bucket* whose target is the amount that
    landed and whose candidates are the payments that could plausibly have settled
    into it. One integer program solves every bucket together under the constraint
    that a payment belongs to at most one settlement - which is what per-settlement
    subset-sum cannot guarantee, however good each individual answer looks.

    Groups produced here *extend* the pairs Stage 1 already found rather than
    replacing them: a payment matched to its invoice, then attributed to a credit,
    becomes one group of three. Every claim asserted by an earlier stage survives,
    which is the monotonicity rule (D.1.4) read correctly - a later stage may add.
    """
    # Every bank credit is a bucket, including ones Stage 1 already linked to the
    # gateway's own settlement record. Those links prove *which payout* a credit is;
    # they say nothing about which payments composed it, and that attribution is the
    # whole job here. Skipping them left Stage 4 looking at 4 credits out of 19.
    credits = [
        row
        for row in sorted(result.rows.values(), key=lambda r: r.txn_id)
        if row.source is SourceKind.BANK
        and row.txn_id not in result.suppressed_ids
        and row.direction is Direction.CREDIT
    ]
    if not credits:
        return [], "no bank credits to attribute"

    attributed = {
        row_id
        for group in result.matches
        for row_id in group.gateway_ids
        if result.rows[row_id].kind is TxnKind.PAYMENT and group.bank_ids
    }
    payments = [
        row
        for row in sorted(result.rows.values(), key=lambda r: r.txn_id)
        if row.source is SourceKind.GATEWAY
        and row.kind is TxnKind.PAYMENT
        and row.txn_id not in result.suppressed_ids
        and row.txn_id not in attributed
    ]
    if not payments:
        return [], "no unattributed payments"

    # Aim at expected NET, not gross. With a gross target the tolerance has to absorb
    # every plausible deduction (6%), which leaves CP-SAT an enormous feasible region:
    # it returned FEASIBLE after ten seconds and could not prove optimality. Netting
    # each payment through the rate card first shrinks the band to rounding noise.
    amounts = {
        row.txn_id: config.rate_card.expected_net(row.amount, row.method).paise
        for row in payments
    }
    gross_by_id = {row.txn_id: row.amount.paise for row in payments}

    buckets: list[Bucket] = []
    for credit in credits:
        landed = credit.settled_on or credit.occurred_on
        candidates = tuple(
            row.txn_id
            for row in payments
            if _could_settle_into(row, landed, config)
        )
        if not candidates:
            continue
        target = credit.amount.paise
        buckets.append(
            Bucket(
                bucket_id=credit.txn_id,
                target=target,
                candidate_ids=candidates,
                # Rounding noise only: each of MDR, GST and TDS is rounded to the
                # paise independently, so a group of n payments can drift by a few
                # paise per payment. Nothing wider is justified once the rate card is
                # applied, and anything wider would let a wrong subset in.
                # `upper` is the only hard bound left: the net sum may fall a few
                # paise below the credit through independent rounding of each fee
                # component, but not meaningfully - money does not appear from
                # nowhere. How far *above* it may sit is what Stage 5 decomposes, so
                # it is scored, not constrained.
                tolerance=Tolerance(lower=0, upper=4 * len(candidates) + 200),
            )
        )

    if not buckets:
        return [], f"{len(credits)} credit(s), none with plausible candidates"

    # Hint: give each payment to the credit whose date matches its *expected*
    # settlement date, falling back to the earliest plausible credit. Hinting every
    # candidate into every bucket - the first attempt - is self-contradictory, since a
    # payment cannot be in two settlements, and CP-SAT gains nothing from a starting
    # point that violates its own constraints.
    expected_bucket: dict[str, str] = {}
    credit_dates = {
        bucket.bucket_id: (
            result.rows[bucket.bucket_id].settled_on
            or result.rows[bucket.bucket_id].occurred_on
        )
        for bucket in buckets
    }
    for bucket in buckets:
        for member in bucket.candidate_ids:
            row = result.rows[member]
            due = config.calendar.settlement_date(
                row.occurred_at, cycle_days=config.settlement_cycle_days
            )
            current = expected_bucket.get(member)
            if current is None:
                expected_bucket[member] = bucket.bucket_id
                continue
            if abs((credit_dates[bucket.bucket_id] - due).days) < abs(
                (credit_dates[current] - due).days
            ):
                expected_bucket[member] = bucket.bucket_id

    hint: dict[str, tuple[str, ...]] = {bucket.bucket_id: () for bucket in buckets}
    for member, bucket_id in sorted(expected_bucket.items()):
        hint[bucket_id] = (*hint[bucket_id], member)
    # A day of disagreement between a payment's expected settlement date and the day
    # the credit landed costs the equivalent of Rs 10,000 of unexplained residual.
    # Large enough to dominate the amount signal, which is nearly flat between
    # adjacent days; far below the attribution bonus, so it never leaves a payment
    # unattributed just to avoid a date penalty.
    day_penalty = 10_00_000
    pair_weights: dict[tuple[str, str], float] = {}
    for bucket in buckets:
        landed = credit_dates[bucket.bucket_id]
        for member in bucket.candidate_ids:
            due = config.calendar.settlement_date(
                result.rows[member].occurred_at, cycle_days=config.settlement_cycle_days
            )
            pair_weights[member, bucket.bucket_id] = float(
                abs((landed - due).days) * day_penalty
            )

    try:
        netting = assign_many_to_one(
            buckets,
            amounts,
            weights=pair_weights,
            hint=hint,
            attribution_weight=config.attribution_weight,
            deterministic_budget=config.solver_budget,
        )
    except InfeasibleAssignment as exc:
        return [], f"no globally consistent attribution: {exc}"

    result.netting = netting
    offenders = check_no_double_use(netting.groups)
    if offenders:
        raise AssertionError(f"solver double-booked: {offenders[:3]}")

    # Stage 4 asserts one relation only: these payments settled into this credit.
    # It deliberately does NOT absorb the invoice links Stage 1 found. Those are a
    # different, and certain, claim - merging them here would republish them at this
    # stage's lower confidence and stop them being auto-postable, which is exactly
    # what happened before groups were made per-relation.
    #
    # The invoices are still carried, because a controller looking at a settlement
    # wants to see what it paid for - but they are carried as membership, not as a
    # re-assertion of the 1:1 links, and `links` stays empty here to say so.
    by_payment: dict[str, MatchGroup] = {}
    for group in result.matches:
        for _gateway_id, ledger_id in group.links:
            for gateway_id in group.gateway_ids:
                by_payment[gateway_id] = group
                _ = ledger_id

    matches: list[MatchGroup] = []
    for attribution in netting.groups:
        credit = result.rows[attribution.bucket_id]
        gateway_ids: set[str] = set(attribution.member_ids)
        ledger_ids: set[str] = set()
        for member in attribution.member_ids:
            existing = by_payment.get(member)
            if existing is not None:
                ledger_ids |= {
                    ledger_id for gateway_id, ledger_id in existing.links if gateway_id == member
                }

        gross = sum(gross_by_id[m] for m in attribution.member_ids)
        explained = gross - credit.amount.paise
        matches.append(
            MatchGroup(
                match_id=make_id(
                    IdKind.MATCH, "stage4", sorted(gateway_ids), attribution.bucket_id
                ),
                stage="stage4.global_assignment",
                gateway_ids=tuple(sorted(gateway_ids)),
                bank_ids=(attribution.bucket_id,),
                ledger_ids=tuple(sorted(ledger_ids)),
                links=(),
                amount=credit.amount,
                confidence=Decimal("0.95") if netting.optimal else Decimal("0.85"),
                reason=(
                    f"{len(attribution.member_ids)} payment(s) totalling "
                    f"{format_inr(Money(gross))} net to the "
                    f"{format_inr(credit.amount)} credit that landed on "
                    f"{credit.settled_on or credit.occurred_on}; "
                    f"{format_inr(Money(explained))} withheld, to be decomposed"
                ),
                evidence=EvidenceBundle(
                    considered=tuple(sorted(gateway_ids | {attribution.bucket_id})),
                    stage="stage4.global_assignment",
                    items=(
                        EvidenceItem(
                            kind="arithmetic",
                            label="gross of the chosen subset",
                            detail=format_inr(Money(gross)),
                            weight=f"{len(attribution.member_ids)} payments",
                        ),
                        EvidenceItem(
                            kind="arithmetic",
                            label="credit that landed",
                            detail=format_inr(credit.amount),
                            source_id=attribution.bucket_id,
                        ),
                        EvidenceItem(
                            kind="arithmetic",
                            label="withheld (decomposed at M10)",
                            detail=format_inr(Money(explained)),
                            weight=f"{Decimal(explained) / Decimal(max(gross, 1)):.4%} of gross",
                        ),
                        EvidenceItem(
                            kind="candidate",
                            label="global consistency",
                            detail=(
                                f"solved with {len(buckets)} settlement(s) at once so no "
                                f"payment serves two credits ({netting.method})"
                            ),
                        ),
                    ),
                ),
            )
        )

    # Optimality and correctness are different claims here, and conflating them would
    # be misleading in both directions. The hard invariant - no payment attributed to
    # two settlements - is guaranteed by *feasibility*, so a FEASIBLE answer is still
    # a globally consistent one. Optimality only decides which of several consistent
    # attributions leaves the least unexplained, and CP-SAT frequently cannot prove
    # that within its budget at this scale. So the status is reported plainly rather
    # than being dressed up as a failure or hidden as a success.
    quality = (
        "proven optimal"
        if netting.optimal
        else "consistent but not proven optimal within the time budget"
    )
    # A credit nobody could explain is the single most important thing on this page,
    # so it becomes a typed exception rather than quietly staying unmatched. Same for
    # a payment that fits no settlement: money that arrived without a home, and money
    # that went out without one, are both findings.
    explained_credits = {m.bank_ids[0] for m in matches if m.bank_ids}
    for credit in credits:
        if credit.txn_id in explained_credits:
            continue
        result.exceptions.append(
            make_exception(
                exception_type=ExceptionType.MISSING_IN_LEDGER,
                reason=(
                    f"{format_inr(credit.amount)} landed on "
                    f"{credit.settled_on or credit.occurred_on} and no subset of the "
                    f"day's payments explains it"
                ),
                amount=credit.amount,
                source_ids=(credit.txn_id,),
                as_of=credit.settled_on or credit.occurred_on,
                evidence=EvidenceBundle(
                    considered=(credit.txn_id,),
                    stage="stage4.global_assignment",
                    abstained_because=(
                        "no combination of unattributed payments nets to this amount "
                        "within the settlement window"
                    ),
                    items=(
                        EvidenceItem(
                            kind="source_span",
                            label="narration",
                            detail=credit.raw_narration or "(none)",
                            source_id=credit.txn_id,
                        ),
                        EvidenceItem(
                            kind="arithmetic",
                            label="amount",
                            detail=format_inr(credit.amount),
                        ),
                    ),
                ),
                suggested_action=(
                    "Check for a payment outside the expected window, an unrecorded "
                    "sale, or a transfer that is not gateway settlement at all."
                ),
            )
        )

    for member in netting.unassigned:
        row = result.rows[member]
        result.exceptions.append(
            make_exception(
                exception_type=ExceptionType.MISSING_IN_BANK,
                reason=(
                    f"{row.external_id} ({format_inr(row.amount)}) captured on "
                    f"{row.occurred_on} was not attributed to any credit that landed"
                ),
                amount=row.amount,
                source_ids=(member,),
                as_of=row.occurred_on,
                evidence=EvidenceBundle(
                    considered=(member,),
                    stage="stage4.global_assignment",
                    abstained_because=(
                        "attributing it would have created more unexplained money "
                        "than the payment is worth"
                    ),
                ),
                suggested_action="Check whether this settlement is still in transit.",
            )
        )

    detail = (
        f"{len(buckets)} bucket(s), {netting.render()}; {quality}; "
        f"{len(credits) - len(explained_credits)} credit(s) unexplained"
    )
    return matches, detail


def _could_settle_into(row: CanonicalTxn, landed: dt.date, config: ReconConfig) -> bool:
    """Could this payment plausibly be part of a credit that landed on that date?"""
    low, high = config.calendar.expected_window(
        row.occurred_at,
        cycle_days=config.settlement_cycle_days,
        slack_days=config.date_slack_days,
    )
    return low <= landed <= high


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
    # Conflicts are judged per *relation*, matching check_no_double_spend. Judging
    # them on raw row overlap refused every Stage 4 settlement attribution outright,
    # because its payments already appear in Stage 1's invoice links - two different
    # claims about the same payment, only one of which is a double-book.
    already: set[tuple[str, str]] = set()
    for group in result.matches:
        already |= group.relation_claims()

    accepted: list[MatchGroup] = []
    conflicts = 0
    for group in matches:
        claims = group.relation_claims()
        if claims & already:
            # Invariant D.1.4: a later stage never silently overwrites an earlier
            # one. The conflict is dropped here and surfaces as an exception.
            conflicts += 1
            continue
        accepted.append(group)
        already |= claims
    result.matches.extend(accepted)
    elapsed = Decimal(str(round((time.perf_counter() - started) * 1000, 3)))
    result.stages.append(
        StageReport(
            stage=stage,
            label=label,
            matches_added=len(accepted),
            rows_consumed=sum(len(g.relation_claims()) for g in accepted),
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

    # --- stage 4: global assignment --------------------------------------
    started = time.perf_counter()
    matches, detail = stage4_global_assignment(result, config)
    _add_stage(result, "stage4", "global assignment", matches, started, detail)

    offenders = result.check_no_double_spend()
    if offenders:
        raise AssertionError(f"double-booking detected: {offenders[:3]}")

    return result
