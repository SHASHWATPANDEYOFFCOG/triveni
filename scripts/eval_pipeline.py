"""Grading the reconciliation pipeline against ground truth.

Deliberately outside `recon/`. The matcher must not be able to see the labels, and
that guarantee is enforced structurally by a test that greps every matching package
for any reference to the ground truth - which only holds if the evaluator lives
somewhere the matcher cannot import from. Keeping it next to the pipeline would have
been convenient and would have made the guarantee unenforceable.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.costmodel import CostModel, OutcomeMix, price
from core.eval import Confusion, MetricRegistry, MetricsReport, source_digest
from core.ids import content_hash
from core.money import sum_money
from ingest.canonical import SourceKind
from recon.normalize import ParseSource
from recon.pipeline import ReconConfig, reconcile
from scripts.truth import true_pairs_from_group

ROOT = Path(__file__).resolve().parent.parent


def cross_source_pairs(
    gateway: list[str], bank: list[str], ledger: list[str]
) -> set[tuple[str, str]]:
    """Every claim that two rows from *different* ledgers belong together.

    Only cross-source pairs count. Two invoices sitting in the same settlement is not
    a reconciliation claim; an invoice matched to a bank credit is.
    """
    pairs: set[tuple[str, str]] = set()
    for left, right in ((gateway, bank), (gateway, ledger), (bank, ledger)):
        for a in left:
            for b in right:
                pairs.add((a, b) if a < b else (b, a))
    return pairs


def evaluate_pipeline(dataset: str | None = None, seed: int = 20260101) -> MetricsReport:
    """Run the pipeline and grade it, pairwise.

    Pairwise precision and recall is the standard record-linkage measure and the only
    one that means anything when groups are many-to-one. An earlier version of this
    function compared a count of match groups against a count of truth groups and
    reported 100% recall, which was arithmetic nonsense: 228 invoice-to-payment pairs
    divided by 17 settlement groups.
    """
    directory = ROOT / "data" / ("seed" if dataset in (None, "seed") else f"generated/{dataset}")
    result = reconcile(directory=directory)

    truth = json.loads((directory / "ground_truth.json").read_text(encoding="utf-8"))
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))

    true_pairs: set[tuple[str, str]] = set()
    for group in truth["groups"]:
        true_pairs |= true_pairs_from_group(group)

    def external(row_id: str) -> str:
        row = result.rows.get(row_id)
        return row.external_id if row else row_id

    # Ask each group what it claims rather than inferring it from co-membership. A
    # settlement group holding 21 payments and 19 invoices asserts 19 payment-to-
    # invoice links, not 399 - see MatchGroup.claimed_pairs, which is written to be
    # read alongside scripts/truth.py::true_pairs_from_group.
    predicted_pairs: set[tuple[str, str]] = set()
    for match in result.matches:
        for a, b in match.claimed_pairs():
            left, right = external(a), external(b)
            predicted_pairs.add((left, right) if left < right else (right, left))

    tp = len(predicted_pairs & true_pairs)
    fp = len(predicted_pairs - true_pairs)
    fn = len(true_pairs - predicted_pairs)
    confusion = Confusion(tp=tp, fp=fp, fn=fn)
    total_rows = len(result.rows)

    # Proposing and posting are different acts, and one precision number for both
    # hides the thing that matters. A match the policy engine routes to a human costs
    # two minutes of review; one it auto-posts and gets wrong corrupts the books and
    # is found weeks later. Stage 4's netting proposals are confident enough to be
    # useful and not confident enough to post unsupervised, which is a legitimate
    # answer - but only if it is reported as one.
    from core.policy import PolicyConfig

    post_threshold = PolicyConfig().min_confidence
    auto_pairs: set[tuple[str, str]] = set()
    for match in result.matches:
        if match.confidence < post_threshold:
            continue
        for a, b in match.claimed_pairs():
            left, right = external(a), external(b)
            auto_pairs.add((left, right) if left < right else (right, left))
    auto_tp = len(auto_pairs & true_pairs)
    auto_fp = len(auto_pairs - true_pairs)

    registry = MetricRegistry()
    registry.count(
        "true_pairs_total",
        len(true_pairs),
        description="cross-source pairs the ground truth says exist (the recall denominator)",
    )
    registry.rate(
        "match_rate",
        result.matched_row_count,
        max(total_rows, 1),
        description="share of ingested rows placed into an accepted match group",
    )
    registry.rate(
        "precision",
        tp,
        max(tp + fp, 1),
        description="of the cross-source pairs we claimed, the share that were true",
    )
    registry.rate(
        "recall",
        tp,
        max(tp + fn, 1),
        description="of the cross-source pairs that exist, the share we claimed",
    )
    registry.scalar("f1", confusion.f1, description="harmonic mean of precision and recall")
    registry.rate(
        "auto_post_precision",
        auto_tp,
        max(auto_tp + auto_fp, 1),
        description=(
            f"precision restricted to matches confident enough to post unsupervised "
            f"(confidence >= {post_threshold}); the rest go to human triage"
        ),
    )
    registry.rate(
        "auto_post_coverage",
        len(auto_pairs),
        max(len(predicted_pairs), 1),
        description="share of proposed pairs that would post without a human",
    )

    # The llm_call_rate denominator is bank statement rows, and the reason is stated
    # so it can be argued with rather than taken on trust: a bank narration is the
    # only place a bank row's matching keys (UTR, rail, counterparty) live. Gateway
    # and ledger rows carry structured order_id / payment_id / counterparty fields and
    # would never be sent to a model at all. The absolute count over every ingested
    # row is reported alongside, so both framings are visible and neither is hidden
    # behind the other.
    narrated = {
        row_id: parse
        for row_id, parse in result.narration_parses.items()
        if result.rows[row_id].source is SourceKind.BANK
        and result.rows[row_id].raw_narration.strip()
    }
    llm_needed = sum(1 for parse in narrated.values() if parse.needs_llm())
    registry.rate(
        "llm_call_rate",
        llm_needed,
        max(len(narrated), 1),
        description=(
            "share of bank narrations a regex could not fully parse; denominator is "
            "bank rows, the only source whose matching keys live in free text"
        ),
        higher_is_better=False,
    )
    registry.count(
        "llm_calls_absolute",
        llm_needed,
        description=f"model calls required across all {total_rows} ingested rows",
        higher_is_better=False,
    )
    registry.count(
        "narrated_rows",
        len(narrated),
        description="bank rows carrying a narration (the llm_call_rate denominator)",
    )
    registry.rate(
        "narration_parse_rate",
        sum(1 for parse in narrated.values() if parse.source is not ParseSource.NONE),
        max(len(narrated), 1),
        description="narrations from which anything at all could be extracted",
    )
    registry.count("rows_ingested", total_rows, description="rows across all three ledgers")
    registry.count(
        "corrupt_rows",
        len(result.corrupt),
        description="rows kept as typed exceptions rather than dropped",
        higher_is_better=False,
    )
    registry.count("exceptions_raised", len(result.exceptions), higher_is_better=False)
    registry.money(
        "unexplained_amount",
        sum_money(row.amount for row in result.unmatched(SourceKind.BANK)),
        description="bank credits not yet attributed to any match group",
    )

    # Wall-clock deliberately does NOT go into metrics.json. `make eval` must be
    # byte-identical across runs, and a timing that varies by a millisecond breaks
    # that. Throughput is `make bench`'s job; accuracy is this one's. Per-stage
    # timings are still carried on ReconResult for the UI's stage ladder.
    # Only pairs Triveni would post *unsupervised* are charged as false matches. A
    # wrong proposal that a human rejects costs two minutes of review; a wrong posting
    # corrupts the books and is found weeks later, which is the ~15x asymmetry the
    # cost model exists to express. Charging every proposal at the posted rate made
    # the run look catastrophically unprofitable while the thing it was pricing -
    # unsupervised error - was zero.
    model = CostModel()
    reviewed = len(predicted_pairs - auto_pairs)
    breakdown = price(
        OutcomeMix(
            rows=total_rows,
            auto_posted=len(auto_pairs),
            true_matches=auto_tp,
            false_matches=auto_fp,
            false_non_matches=fn,
            needs_review=reviewed + len(result.exceptions),
            exposed_by_false_matches=sum_money(match.amount for match in result.matches[:auto_fp]),
        ),
        model,
    )
    registry.money("cost_of_being_wrong", breakdown.total)
    registry.money("saved_vs_manual", breakdown.saved_vs_manual, higher_is_better=True)

    return MetricsReport(
        dataset=manifest["name"],
        dataset_rows=total_rows,
        dataset_digest=manifest["digest"],
        seed=seed,
        metrics=tuple(registry.metrics),
        cost=breakdown,
        cost_model=model,
        confusion=confusion,
        notes=(
            "Stages 0-1 only (M6). Blocking, Fellegi-Sunter, the global solver and the "
            "LLM residue stage land at M7-M11. Stage 1 is meant to be low-recall: it "
            "makes only the claims that cannot be wrong, leaving a smaller and harder "
            "problem for the solver. That is why precision is 1.0 and recall is not.",
            "Wall-clock is excluded by design so this file stays byte-identical across "
            "runs; see `make bench` for throughput.",
            f"config digest: {content_hash(ReconConfig().canonical())[:16]}",
        ),
        code_digest=source_digest(),
    )
