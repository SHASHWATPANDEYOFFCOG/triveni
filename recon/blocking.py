"""Stage 2: candidate generation.

Comparing every row against every other row is quadratic and pointless - on the full
dataset that is roughly 15 million pairs, of which about 25 thousand are real. Blocking
is the step that decides which pairs are *worth scoring*, and it is judged on two
numbers that trade against each other:

* **pair completeness (PC)** - the share of true pairs that survive into the candidate
  set. Anything blocking discards, no downstream stage can ever recover, so PC is a
  hard ceiling on the recall of the entire pipeline.
* **reduction ratio (RR)** - the share of all possible pairs that blocking eliminated.
  This is what buys the compute budget for Fellegi-Sunter and the solver.

Both are reported per strategy in the README table, because a blocker with PC 0.99 and
RR 0.5 and one with PC 0.99 and RR 0.999 are very different pieces of engineering and
a single "it works" hides that.

**Two families, unioned.**

*Deterministic* blocks come from things that must agree if two rows are the same
event: a shared reference, a settlement window that overlaps, a shared counterparty
token. These are cheap, exact, and explainable - a human can be told "these were
compared because they share UTR 940235305794".

*Dense* blocks come from character n-gram embeddings of the narration and counterparty,
compared by cosine similarity. They catch the cases rules miss: a truncated bank name,
a transliteration variant, a narration whose format we have never seen. The literature
on blocking for entity matching (Thirumuruganathan et al., VLDB 2021, and the VLDB
experimental analyses of pre-trained embeddings for ER) consistently finds dense
blocking lifts recall over rule-only blocking at comparable cost; we report our own
measured PC/RR rather than borrowing their numbers.

**Why the embedding is local.** No `torch`, no `sentence-transformers`, no download.
The vectoriser here is a hashed character n-gram projection - deterministic, ~40 lines,
runs in milliseconds, and works on a laptop with no network. That is a deliberate
trade: a transformer encoder would very likely score better on paraphrase, but bank
narrations are not paraphrase, they are formatting noise and truncation, which
character n-grams handle well. The neural slot stays feature-flagged
(`TRIVENI_ENABLE_NEURAL_EMBEDDINGS`) and off, and the honest comparison is in
`docs/limitations.md`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, Protocol

import numpy as np

from core.clock import INDIAN_BANK_CALENDAR, SettlementCalendar
from ingest.canonical import CanonicalTxn, SourceKind, TxnKind
from recon.normalize import counterparty_tokens, name_key

#: Dimensionality of the hashed n-gram projection. 256 is enough to keep collisions
#: rare across a few thousand short strings and small enough to stay fast.
EMBEDDING_DIM: Final = 256
NGRAM_SIZE: Final = 3

Pair = tuple[str, str]


def _ordered(a: str, b: str) -> Pair:
    return (a, b) if a < b else (b, a)


# --------------------------------------------------------------------------- #
# Strategies
# --------------------------------------------------------------------------- #
class BlockingStrategy(Protocol):
    """Anything that can propose candidate pairs, and say why.

    ``name`` and ``description`` are read-only properties rather than plain
    attributes so that frozen dataclasses satisfy the protocol: a strategy that could
    be mutated after construction would make the PC/RR table describe something other
    than what ran.
    """

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    def candidates(self, rows: Sequence[CanonicalTxn]) -> set[Pair]:
        """Pairs worth scoring. Cross-source only."""
        ...


@dataclass(frozen=True, slots=True)
class KeyBlocker:
    """A deterministic blocker: rows sharing any key become candidates.

    ``max_block_size`` is a safety valve, not an optimisation. A key that lands
    thousands of rows in one block (an empty string, a default counterparty) silently
    reintroduces the quadratic blow-up blocking exists to prevent, so an oversized
    block is dropped and *reported* rather than quietly expanded.
    """

    name: str
    description: str
    key_fn: object  # Callable[[CanonicalTxn], set[str]]
    max_block_size: int = 400
    applies_to: frozenset[frozenset[SourceKind]] = field(default_factory=frozenset)
    """Which source-pair relations this blocker is *for*. Empty means all.

    This matters more than it looks. Every blocker was originally allowed to propose
    every cross-source pair, and the settlement-window blocker - which exists for the
    many-to-one netting relation - was consequently emitting gateway-to-ledger pairs
    that the reference blocker already covers exactly. That cost 16,000 candidate
    pairs and dropped the union's reduction ratio to 0.57 while adding no pair
    completeness at all. Naming the relation each blocker serves is not an
    optimisation; it is the difference between a blocking *scheme* and four blockers
    that happen to run.
    """

    def _wanted(self, left: CanonicalTxn, right: CanonicalTxn) -> bool:
        if left.source is right.source:
            return False
        if not self.applies_to:
            return True
        return frozenset({left.source, right.source}) in self.applies_to

    def candidates(self, rows: Sequence[CanonicalTxn]) -> set[Pair]:
        buckets: dict[str, list[CanonicalTxn]] = defaultdict(list)
        for row in rows:
            for key in self.key_fn(row):  # type: ignore[operator]
                if key:
                    buckets[key].append(row)

        pairs: set[Pair] = set()
        for key in sorted(buckets):
            block = buckets[key]
            if len(block) > self.max_block_size:
                continue
            for i, left in enumerate(block):
                for right in block[i + 1 :]:
                    if self._wanted(left, right):
                        pairs.add(_ordered(left.txn_id, right.txn_id))
        return pairs

    def oversized_blocks(self, rows: Sequence[CanonicalTxn]) -> list[tuple[str, int]]:
        buckets: dict[str, int] = defaultdict(int)
        for row in rows:
            for key in self.key_fn(row):  # type: ignore[operator]
                if key:
                    buckets[key] += 1
        return sorted(
            ((key, size) for key, size in buckets.items() if size > self.max_block_size),
            key=lambda item: -item[1],
        )


# --- key functions ---------------------------------------------------------
def reference_keys(row: CanonicalTxn) -> set[str]:
    """Shared reference: UTR, RRN, order id. The strongest signal there is."""
    keys: set[str] = set()
    if row.reference:
        keys.add(f"ref:{row.reference}")
    utr = str(row.metadata.get("utr") or "")
    if utr:
        keys.add(f"ref:{utr}")
    if row.parent_id:
        keys.add(f"ref:{row.parent_id}")
    return keys


def settlement_window_keys(
    row: CanonicalTxn,
    *,
    calendar: SettlementCalendar = INDIAN_BANK_CALENDAR,
    cycle_days: int = 2,
    slack_days: int = 1,
) -> set[str]:
    """Every date on which this row could plausibly meet its counterpart.

    This is the blocker that makes many-to-one settlement matching tractable. A bank
    credit cannot be amount-blocked against the individual payments that compose it -
    it is their *net* - so the thing they share is the settlement date. Emitting the
    whole plausible band rather than a single date is what stops a long weekend or a
    one-day bank delay from destroying pair completeness.
    """
    # A bank credit and a gateway *settlement* record are both already dated at the
    # settlement itself, so they anchor on their own date. Only a payment or an
    # invoice needs the T+2 projection - applying it to a settlement row would look
    # for the credit two business days after the payout, which is two days after it
    # actually landed, and cost real pair completeness on split settlements.
    if row.source is SourceKind.BANK or row.kind is TxnKind.SETTLEMENT:
        anchor = row.settled_on or row.occurred_on
        days = {anchor}
        cursor = anchor
        for _ in range(slack_days + 1):
            cursor = cursor - dt.timedelta(days=1)
            days.add(cursor)
            days.add(anchor + dt.timedelta(days=len(days) - 1))
        return {f"win:{day.isoformat()}" for day in days}

    low, high = calendar.expected_window(
        row.occurred_at, cycle_days=cycle_days, slack_days=slack_days
    )
    days = set()
    cursor = low
    while cursor <= high:
        days.add(cursor)
        cursor += dt.timedelta(days=1)
    return {f"win:{day.isoformat()}" for day in days}


def counterparty_keys(row: CanonicalTxn) -> set[str]:
    """The whole normalised name, and its phonetic key for transliteration variants.

    Individual *tokens* were the first implementation and they do not scale. With a
    few thousand rows drawn from a realistic name distribution, a block keyed on
    ``TEXTILES`` or ``TRADERS`` collects hundreds of unrelated companies: on the
    5,000-row dataset that single choice produced 655,122 candidate pairs - 96% of the
    entire union - while contributing no pair completeness that the reference and
    amount blockers had not already found.

    Whole-name keys are selective, and the cases they miss (a bank that truncated the
    name to twenty characters, a transliteration variant) are exactly what the
    phonetic key and the dense blocker are for. Layered coverage, each layer cheap.
    """
    keys: set[str] = set()
    normalised = " ".join(sorted(counterparty_tokens(row.counterparty)))
    if normalised:
        keys.add(f"cp:{normalised}")
    key = name_key(row.counterparty)
    if key:
        keys.add(f"ph:{key}")
    return keys


def amount_keys(row: CanonicalTxn) -> set[str]:
    """Exact amount. An invoice and its payment agree to the paise.

    An earlier version also emitted log-scale buckets either side, on the theory that
    they would survive a small fee deduction. Measured on the seed, the buckets added
    **10,236 candidate pairs for zero additional pair completeness** - every true pair
    they caught was already caught by the exact key or by the reference blocker. They
    are gone. A blocking key that widens the candidate set without widening coverage
    is pure cost, and the only way to know which is which is to measure each strategy
    separately, which is what the PC/RR table is for.
    """
    paise = abs(row.amount.paise)
    return {f"amt:{paise}"} if paise else set()


DETERMINISTIC_STRATEGIES: tuple[KeyBlocker, ...] = (
    KeyBlocker(
        name="reference",
        description="shared UTR / RRN / order id after normalisation",
        key_fn=reference_keys,
    ),
    KeyBlocker(
        name="settlement_window",
        description="settlement dates overlapping under the Indian bank calendar (netting relation only)",
        key_fn=settlement_window_keys,
        max_block_size=4_000,
        # Only relations that involve the bank. A gateway payment and a ledger
        # invoice are linked by their order id, not by sharing a settlement date -
        # and letting this blocker propose those pairs too added 16,000 candidates
        # for zero extra pair completeness.
        applies_to=frozenset(
            {
                frozenset({SourceKind.GATEWAY, SourceKind.BANK}),
                frozenset({SourceKind.LEDGER, SourceKind.BANK}),
            }
        ),
    ),
    KeyBlocker(
        name="counterparty",
        description="whole normalised counterparty name, or its phonetic key",
        key_fn=counterparty_keys,
    ),
    KeyBlocker(
        name="amount",
        description="exact amount (1:1 relation only)",
        key_fn=amount_keys,
        # A bank credit is the *net* of many payments, so its amount equals no single
        # payment's. Amount blocking is a 1:1 device and saying so keeps it honest.
        applies_to=frozenset({frozenset({SourceKind.GATEWAY, SourceKind.LEDGER})}),
    ),
)


# --------------------------------------------------------------------------- #
# Dense blocking
# --------------------------------------------------------------------------- #
def _ngrams(text: str, size: int = NGRAM_SIZE) -> list[str]:
    padded = f"  {text.lower().strip()}  "
    return [padded[i : i + size] for i in range(len(padded) - size + 1)]


def _hash_index(token: str, dim: int) -> int:
    """Stable across processes and machines - unlike ``hash()``, which is salted."""
    return int.from_bytes(hashlib.blake2b(token.encode(), digest_size=4).digest(), "big") % dim


def embed(text: str, dim: int = EMBEDDING_DIM) -> np.ndarray:
    """Hashed character n-gram vector, L2-normalised.

    Deterministic, dependency-free and fast. Character n-grams are the right family
    for this data: bank narrations differ by truncation, casing and punctuation far
    more often than by wording, and n-grams are robust to exactly that.
    """
    vector = np.zeros(dim, dtype=np.float32)
    for gram in _ngrams(text):
        vector[_hash_index(gram, dim)] += 1.0
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 0 else vector


def _text_for(row: CanonicalTxn) -> str:
    """What the embedding sees: the narration if there is one, else the name."""
    return (row.raw_narration or row.counterparty or row.reference or "").strip()


@dataclass(frozen=True, slots=True)
class DenseBlocker:
    """Cosine top-k over hashed character n-grams, per source pair.

    Runs source-pair by source-pair rather than over one big matrix, because only
    cross-source pairs are reconciliation claims and the block-diagonal work would be
    thrown away.
    """

    name: str = "dense_ann"
    description: str = "cosine top-k over hashed character 3-gram embeddings"
    top_k: int = 8
    min_similarity: float = 0.30
    dim: int = EMBEDDING_DIM

    def candidates(self, rows: Sequence[CanonicalTxn]) -> set[Pair]:
        by_source: dict[SourceKind, list[CanonicalTxn]] = defaultdict(list)
        for row in rows:
            if _text_for(row):
                by_source[row.source].append(row)

        pairs: set[Pair] = set()
        sources = sorted(by_source, key=lambda s: s.value)
        for i, left_source in enumerate(sources):
            for right_source in sources[i + 1 :]:
                pairs |= self._cross(by_source[left_source], by_source[right_source])
        return pairs

    def _cross(self, left: list[CanonicalTxn], right: list[CanonicalTxn]) -> set[Pair]:
        if not left or not right:
            return set()
        left_matrix = np.vstack([embed(_text_for(r), self.dim) for r in left])
        right_matrix = np.vstack([embed(_text_for(r), self.dim) for r in right])
        similarity = left_matrix @ right_matrix.T

        k = min(self.top_k, right_matrix.shape[0])
        pairs: set[Pair] = set()
        # argpartition is O(n) per row where a full sort is O(n log n); at 5,000 rows
        # that is the difference between the blocker being free and being the
        # bottleneck it exists to remove.
        top = np.argpartition(-similarity, kth=k - 1, axis=1)[:, :k]
        for i, row_indices in enumerate(top):
            for j in row_indices:
                if similarity[i, j] >= self.min_similarity:
                    pairs.add(_ordered(left[i].txn_id, right[int(j)].txn_id))
        return pairs


# --------------------------------------------------------------------------- #
# Candidate set
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class StrategyReport:
    """PC and RR for one strategy, plus what it cost."""

    name: str
    description: str
    candidate_pairs: int
    reduction_ratio: Decimal
    pair_completeness: Decimal | None = None
    oversized_blocks: int = 0

    def render(self) -> str:
        pc = "-" if self.pair_completeness is None else f"{self.pair_completeness:.4f}"
        return (
            f"{self.name:20} {self.candidate_pairs:>10,}  RR {self.reduction_ratio:.6f}  PC {pc}"
        )


@dataclass(slots=True)
class CandidateSet:
    """The pairs Stage 3 will actually score, and which blocker proposed each."""

    pairs: set[Pair] = field(default_factory=set)
    by_strategy: dict[str, set[Pair]] = field(default_factory=dict)
    total_possible: int = 0
    reports: list[StrategyReport] = field(default_factory=list)

    @property
    def reduction_ratio(self) -> Decimal:
        if not self.total_possible:
            return Decimal(0)
        return Decimal(1) - Decimal(len(self.pairs)) / Decimal(self.total_possible)

    def why(self, pair: Pair) -> list[str]:
        """Which strategies proposed this pair. Explainability, all the way down."""
        return sorted(name for name, pairs in self.by_strategy.items() if pair in pairs)


def possible_cross_source_pairs(rows: Sequence[CanonicalTxn]) -> int:
    """How many comparisons a brute-force matcher would make.

    The RR denominator. Cross-source only, because within-source pairs were never
    candidates in the first place and counting them would flatter every strategy.
    """
    counts: dict[SourceKind, int] = defaultdict(int)
    for row in rows:
        counts[row.source] += 1
    sources = sorted(counts, key=lambda s: s.value)
    total = 0
    for i, left in enumerate(sources):
        for right in sources[i + 1 :]:
            total += counts[left] * counts[right]
    return total


def generate_candidates(
    rows: Sequence[CanonicalTxn],
    strategies: Iterable[BlockingStrategy] | None = None,
    *,
    include_dense: bool = True,
) -> CandidateSet:
    """Union of every strategy's proposals.

    Union, not intersection: blocking's job is to lose as few true pairs as possible,
    and each strategy covers a different failure mode. Precision is Stage 3 and Stage
    4's problem, and they are much better at it than a rule is.
    """
    if strategies is None:
        chosen: list[BlockingStrategy] = list(DETERMINISTIC_STRATEGIES)
        if include_dense:
            chosen.append(DenseBlocker())
    else:
        chosen = list(strategies)

    result = CandidateSet(total_possible=possible_cross_source_pairs(rows))
    for strategy in chosen:
        pairs = strategy.candidates(rows)
        result.by_strategy[strategy.name] = pairs
        result.pairs |= pairs
        oversized = (
            len(strategy.oversized_blocks(rows)) if isinstance(strategy, KeyBlocker) else 0
        )
        result.reports.append(
            StrategyReport(
                name=strategy.name,
                description=strategy.description,
                candidate_pairs=len(pairs),
                reduction_ratio=(
                    Decimal(1) - Decimal(len(pairs)) / Decimal(result.total_possible)
                    if result.total_possible
                    else Decimal(0)
                ),
                oversized_blocks=oversized,
            )
        )
    result.reports.append(
        StrategyReport(
            name="union (all)",
            description="every strategy combined - what Stage 3 actually scores",
            candidate_pairs=len(result.pairs),
            reduction_ratio=result.reduction_ratio,
        )
    )
    return result


def relevant_rows(rows: Iterable[CanonicalTxn]) -> list[CanonicalTxn]:
    """Rows worth blocking at all.

    Gateway *settlement* rows are the gateway's own summary of a payout; they meet
    bank credits, not invoices. Including every kind in every block would inflate the
    candidate count without adding a true pair.
    """
    return sorted(
        (r for r in rows if r.kind in {TxnKind.PAYMENT, TxnKind.INVOICE, TxnKind.SETTLEMENT}),
        key=lambda r: r.txn_id,
    )
