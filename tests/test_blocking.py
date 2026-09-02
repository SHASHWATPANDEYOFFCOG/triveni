"""M7 gate: candidate generation, measured with pair completeness and reduction ratio.

The DoD is PC >= 0.99 at a reported RR. PC is the number that matters most in the
whole pipeline: anything blocking discards, no later stage can recover, so it is a
hard ceiling on final recall. RR is what pays for Fellegi-Sunter and the solver.

The tests below also pin the two lessons that came out of measuring each strategy
separately rather than declaring the blocker "done":

* a key that widens the candidate set without widening coverage is pure cost - the
  log-scale amount buckets added 10,236 pairs for zero PC and were deleted;
* token-level counterparty keys do not scale - `cp:TEXTILES` collected 655,122 pairs
  on the 5,000-row dataset, 96% of the whole union, for coverage the reference blocker
  already had.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from ingest.canonical import SourceKind, TxnKind
from recon.blocking import (
    DETERMINISTIC_STRATEGIES,
    DenseBlocker,
    KeyBlocker,
    amount_keys,
    counterparty_keys,
    embed,
    generate_candidates,
    possible_cross_source_pairs,
    relevant_rows,
    settlement_window_keys,
)
from scripts.blocking_report import build, load_rows

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "data" / "seed"
FULL_DIR = ROOT / "data" / "generated" / "full"


@pytest.fixture(scope="module")
def seed_rows():
    return relevant_rows(load_rows(SEED_DIR))


@pytest.fixture(scope="module")
def seed_report():
    return build(SEED_DIR)


# --------------------------------------------------------------------------- #
# The DoD
# --------------------------------------------------------------------------- #
def test_pair_completeness_is_at_least_ninety_nine_percent(seed_report) -> None:
    candidates, truth, _possible, _rows = seed_report
    union = next(r for r in candidates.reports if r.name == "union (all)")
    assert union.pair_completeness is not None
    assert union.pair_completeness >= Decimal("0.99"), (
        f"PC {union.pair_completeness} caps final recall below the DoD"
    )


def test_reduction_ratio_is_reported_and_substantial(seed_report) -> None:
    candidates, _truth, _possible, _rows = seed_report
    union = next(r for r in candidates.reports if r.name == "union (all)")
    assert union.reduction_ratio > Decimal("0.85")


def test_every_strategy_reports_its_own_pc_and_rr(seed_report) -> None:
    """One number for the whole blocker would hide which strategy is doing the work."""
    candidates, _truth, _possible, _rows = seed_report
    named = {r.name for r in candidates.reports}
    assert named == {
        "reference",
        "settlement_window",
        "counterparty",
        "amount",
        "dense_ann",
        "union (all)",
    }
    for report in candidates.reports:
        assert report.pair_completeness is not None
        assert Decimal(0) <= report.reduction_ratio <= Decimal(1)
        assert report.description


@pytest.mark.slow
def test_pair_completeness_holds_at_full_scale() -> None:
    """A blocker that only works on 500 rows is not a blocker."""
    if not FULL_DIR.exists():
        pytest.skip("run `python -m data.gen --spec full` first")
    candidates, truth, possible, rows = build(FULL_DIR)
    union = next(r for r in candidates.reports if r.name == "union (all)")
    assert union.pair_completeness is not None
    assert union.pair_completeness >= Decimal("0.99")
    assert union.reduction_ratio > Decimal("0.98"), (
        "RR must improve with scale, not degrade - the candidate set is the compute budget"
    )
    assert possible > 5_000_000


# --------------------------------------------------------------------------- #
# Individual strategies
# --------------------------------------------------------------------------- #
def test_reference_blocking_is_extremely_selective(seed_report) -> None:
    candidates, _truth, _possible, _rows = seed_report
    report = next(r for r in candidates.reports if r.name == "reference")
    assert report.reduction_ratio > Decimal("0.99")


def test_settlement_window_carries_the_many_to_one_relation(seed_report) -> None:
    """A bank credit is the *net* of many payments, so it can never be amount-blocked
    against them. The date is the only thing they share."""
    candidates, _truth, _possible, _rows = seed_report
    window = next(r for r in candidates.reports if r.name == "settlement_window")
    amount = next(r for r in candidates.reports if r.name == "amount")
    assert window.pair_completeness is not None and amount.pair_completeness is not None
    assert window.pair_completeness > amount.pair_completeness


def test_settlement_rows_anchor_on_their_own_date() -> None:
    """A settlement record is already dated at the settlement. Projecting T+2 from it
    looks for the credit two days after it landed, and costs real completeness."""
    import datetime as dt

    from core.clock import IST
    from core.money import Money
    from ingest.canonical import Direction, build_txn

    settlement = build_txn(
        source=SourceKind.GATEWAY,
        kind=TxnKind.SETTLEMENT,
        external_id="setl_1",
        amount=Money(1000),
        direction=Direction.CREDIT,
        occurred_at=dt.datetime(2026, 3, 10, 17, 30, tzinfo=IST),
    )
    keys = settlement_window_keys(settlement)
    assert "win:2026-03-10" in keys


def test_amount_blocking_uses_the_exact_amount_only() -> None:
    """The log-scale buckets added 10,236 candidate pairs for zero pair completeness.
    A key that widens the candidate set without widening coverage is pure cost."""
    import datetime as dt

    from core.clock import IST
    from core.money import Money
    from ingest.canonical import Direction, build_txn

    row = build_txn(
        source=SourceKind.LEDGER,
        kind=TxnKind.INVOICE,
        external_id="INV-1",
        amount=Money(417_808),
        direction=Direction.CREDIT,
        occurred_at=dt.datetime(2026, 3, 2, tzinfo=IST),
    )
    assert amount_keys(row) == {"amt:417808"}


def test_counterparty_blocking_keys_on_the_whole_name() -> None:
    """Token keys do not scale: `cp:TEXTILES` collected 655,122 pairs on the
    5,000-row dataset for coverage the reference blocker already had."""
    import datetime as dt

    from core.clock import IST
    from core.money import Money
    from ingest.canonical import Direction, build_txn

    def row(name: str):
        return build_txn(
            source=SourceKind.LEDGER,
            kind=TxnKind.INVOICE,
            external_id=f"INV-{name}",
            amount=Money(1),
            direction=Direction.CREDIT,
            occurred_at=dt.datetime(2026, 3, 2, tzinfo=IST),
            counterparty=name,
        )

    keys = counterparty_keys(row("Pillai Hardware Pvt Ltd"))
    assert any(k.startswith("cp:") for k in keys)
    # Two companies sharing only a trade word must not share a key.
    assert not (counterparty_keys(row("Pillai Textiles")) & counterparty_keys(row("Nair Textiles")))
    # The same company spelled two ways must share one.
    assert counterparty_keys(row("Pillai Hardware Pvt Ltd")) & counterparty_keys(
        row("PILLAI HARDWARE PRIVATE LIMITED")
    )


def test_blockers_only_propose_the_relation_they_serve(seed_rows) -> None:
    """Every blocker proposing every relation is four blockers that happen to run,
    not a blocking scheme."""
    window = next(s for s in DETERMINISTIC_STRATEGIES if s.name == "settlement_window")
    by_id = {r.txn_id: r for r in seed_rows}
    for left, right in window.candidates(seed_rows):
        sources = {by_id[left].source, by_id[right].source}
        assert SourceKind.BANK in sources, sources


def test_a_blocker_never_proposes_a_within_source_pair(seed_rows) -> None:
    by_id = {r.txn_id: r for r in seed_rows}
    candidates = generate_candidates(seed_rows)
    for left, right in candidates.pairs:
        assert by_id[left].source is not by_id[right].source


def test_oversized_blocks_are_dropped_and_reported(seed_rows) -> None:
    """A key that lands thousands of rows in one block silently reintroduces the
    quadratic blow-up blocking exists to prevent."""
    everything = KeyBlocker(
        name="degenerate",
        description="one block for every row",
        key_fn=lambda row: {"same"},
        max_block_size=5,
    )
    assert everything.candidates(seed_rows) == set()
    assert everything.oversized_blocks(seed_rows)[0][1] == len(seed_rows)


# --------------------------------------------------------------------------- #
# Dense blocking
# --------------------------------------------------------------------------- #
def test_embeddings_are_deterministic_across_processes() -> None:
    """Uses blake2b rather than `hash()`, which is salted per process - so the same
    narration embeds identically on any machine."""
    import numpy as np

    first = embed("NEFT-UTIB940235305794-RAZORPAY SOFTWARE PVT LTD-SETTLEMENT")
    second = embed("NEFT-UTIB940235305794-RAZORPAY SOFTWARE PVT LTD-SETTLEMENT")
    assert np.array_equal(first, second)
    assert abs(float(np.linalg.norm(first)) - 1.0) < 1e-6


def test_embeddings_are_robust_to_truncation_and_case() -> None:
    """The failure mode in real statements is not paraphrase, it is truncation and
    casing - which is exactly what character n-grams handle."""

    full = embed("Sethi Hardware Private Limited")
    truncated = embed("SETHI HARDWARE PRIV")
    unrelated = embed("Krishnan Chemicals LLP")
    assert float(full @ truncated) > float(full @ unrelated)


def test_dense_blocking_needs_no_network_or_download() -> None:
    """`make demo` must run cold and offline; a transformer encoder would not."""
    import sys

    assert "torch" not in sys.modules
    assert "sentence_transformers" not in sys.modules


def test_dense_blocker_output_is_linear_not_quadratic(seed_rows) -> None:
    """The guarantee `top_k` actually gives is a bound on *out*-degree, so the
    candidate count grows with the number of rows rather than with their square.

    In-degree is deliberately unbounded: there are only ~20 bank rows and hundreds of
    gateway rows, so one credit legitimately appears in many rows' top-k. Asserting a
    per-row cap in both directions would be asserting something the algorithm does not
    promise and should not.
    """
    small = DenseBlocker(top_k=2, min_similarity=0.0).candidates(seed_rows)
    large = DenseBlocker(top_k=8, min_similarity=0.0).candidates(seed_rows)
    assert small <= large, "raising top_k must only ever add candidates"
    assert len(small) <= 2 * len(seed_rows) * 3
    assert len(large) < possible_cross_source_pairs(seed_rows)


def test_dense_blocker_similarity_floor_prunes(seed_rows) -> None:
    permissive = DenseBlocker(top_k=8, min_similarity=0.0).candidates(seed_rows)
    strict = DenseBlocker(top_k=8, min_similarity=0.9).candidates(seed_rows)
    assert strict <= permissive
    assert len(strict) < len(permissive)


# --------------------------------------------------------------------------- #
# Determinism and bookkeeping
# --------------------------------------------------------------------------- #
def test_candidate_generation_is_deterministic(seed_rows) -> None:
    assert generate_candidates(seed_rows).pairs == generate_candidates(seed_rows).pairs


def test_a_candidate_can_say_which_strategies_proposed_it(seed_rows) -> None:
    """Explainability all the way down: a human can be told *why* two rows were
    compared, not just that they were."""
    candidates = generate_candidates(seed_rows)
    pair = next(iter(sorted(candidates.pairs)))
    assert candidates.why(pair), "no strategy claims a pair it produced"


def test_the_rr_denominator_counts_only_cross_source_comparisons(seed_rows) -> None:
    """Counting within-source pairs would flatter every strategy."""
    possible = possible_cross_source_pairs(seed_rows)
    total = len(seed_rows) * (len(seed_rows) - 1) // 2
    assert 0 < possible < total


def test_blocking_is_wired_into_the_pipeline_as_stage_two() -> None:
    from recon.pipeline import reconcile

    result = reconcile()
    stages = [s.stage for s in result.stages]
    assert stages == ["stage0", "stage1", "stage2", "stage3"]
    assert result.candidates is not None
    assert result.candidates.pairs, "Stage 2 produced no candidates for Stage 3"
    detail = next(s for s in result.stages if s.stage == "stage2").detail
    assert "reduction ratio" in detail


def test_stage_two_blocks_everything_but_only_open_pairs_are_matchable() -> None:
    """Two different sets, for two different jobs.

    `candidates.pairs` covers *every* relevant row, including ones Stage 1 already
    matched, because Stage 3 fits its EM model on this set and the residue is a badly
    biased sample of it - all the easy true matches have been removed. Fitting on the
    residue taught the model that landing in the expected settlement window was
    evidence *against* a match.

    `open_pairs` is the subset a later stage may actually turn into a match, and that
    is what keeps the stage ladder honest.
    """
    from recon.pipeline import reconcile

    result = reconcile()
    assert result.candidates is not None
    assert result.open_pairs <= result.candidates.pairs
    assert len(result.open_pairs) < len(result.candidates.pairs)

    # open_pairs is a snapshot taken at Stage 2, so it is compared against what
    # Stage 1 had consumed at that moment - not against the final consumed set, which
    # also contains whatever Stage 3 went on to match out of these very pairs.
    consumed_by_stage_one = {
        row_id
        for match in result.matches
        if match.stage.startswith("stage1")
        for row_id in match.all_ids
    }
    for left, right in result.open_pairs:
        assert left not in consumed_by_stage_one
        assert right not in consumed_by_stage_one
