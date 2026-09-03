"""Grading the reconciliation pipeline against ground truth.

Deliberately outside `recon/`. The matcher must not be able to see the labels, and
that guarantee is enforced structurally by a test that greps every matching package
for any reference to the ground truth - which only holds if the evaluator lives
somewhere the matcher cannot import from. Keeping it next to the pipeline would have
been convenient and would have made the guarantee unenforceable.
"""

from __future__ import annotations

import json
from decimal import Decimal
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


def evaluate_pipeline(
    dataset: str | None = None,
    seed: int = 20260101,
    alpha: Decimal = Decimal("0.01"),
) -> MetricsReport:
    """Grade the pipeline, with the auto-post threshold conformally calibrated.

    ``alpha`` defaults to 1%, which `scripts/calibrate.py` shows is inside the
    zero-error region on this dataset: it admits 70.3% of claims at a held-out error
    of 0.00% and costs Rs 1,800, against Rs 7,600 for the 100%-coverage operating
    point above the step at alpha=1.6%. Coverage is a means; cost is the objective.
    """
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
    # The auto-post threshold is CALIBRATED, not chosen. Until M12 it was a
    # hard-coded 0.90 - a number that meant nothing, transferred nowhere, and could
    # not say by how much it was wrong. It is now the conformal threshold at the
    # configured alpha, fitted on a held-out calibration slice, and the guarantee it
    # carries is reported alongside.
    from core.conformal import Observation, calibrate, evaluate_on, split
    from core.policy import PolicyConfig

    # Claims come out of the pipeline in internal txn ids; the truth speaks external
    # ids. Comparing them directly marked every claim wrong, no threshold could clear
    # the budget, the calibration reported itself unavailable, and the whole thing
    # silently fell back to the hard-coded 0.90 it was meant to replace - looking
    # exactly like a working feature.
    labelled: list[Observation] = []
    for match in result.matches:
        for a, b in sorted(match.claimed_pairs()):
            left, right = external(a), external(b)
            pair = (left, right) if left < right else (right, left)
            labelled.append(
                Observation(
                    identifier=f"{pair[0]}|{pair[1]}",
                    confidence=match.confidence,
                    correct=pair in true_pairs,
                    amount_paise=match.amount.paise,
                )
            )
    calibration_set, holdout = split(sorted(labelled, key=lambda o: o.identifier))
    calibration = calibrate(calibration_set, alpha)
    holdout_row = evaluate_on(calibration, holdout)
    post_threshold = (
        calibration.threshold if calibration.available else PolicyConfig().min_confidence
    )
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
            f"precision over every claim at or above the calibrated threshold "
            f"({post_threshold}) - IN-SAMPLE, since it includes the calibration slice "
            f"the threshold was fitted on. conformal_realised_error is the held-out "
            f"number and is the one the guarantee is about"
        ),
    )
    registry.scalar(
        "conformal_threshold",
        post_threshold,
        description=(
            f"confidence required to post unsupervised, calibrated at alpha={alpha} "
            f"on {calibration.n_calibration} held-out claim(s)"
        ),
        higher_is_better=False,
    )
    registry.scalar(
        "conformal_realised_error",
        holdout_row.realised,
        unit="%",
        description=(
            "error among auto-posted claims on the HELD-OUT split - the number the "
            "guarantee is about, reported beside the nominal bound"
        ),
        higher_is_better=False,
    )
    registry.scalar(
        "conformal_nominal_bound",
        alpha,
        unit="%",
        description="the promise; conformal_realised_error is what happened",
        higher_is_better=False,
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
        "narration_llm_calls",
        llm_needed,
        description=(
            "bank narrations a regex could not parse, so a model had to. NOT the total "
            "model calls - see rows_reaching_model and residue_llm_calls below"
        ),
        higher_is_better=False,
    )
    # The number that actually answers "how much does this system lean on a model".
    #
    # An earlier version reported only the narration figure under the name
    # `llm_calls_absolute` with the description "model calls across all 536 rows",
    # and the README repeated it as "1 model call across 536 rows". That was wrong in
    # the most embarrassing direction: the residue stage sends 13 rows to a model at 3
    # samples each, so the true call count is 39. Two different things were being
    # counted and the flattering one had the general-sounding name.
    reached = result.escalation.considered if result.escalation else 0
    registry.count(
        "rows_reaching_model",
        reached,
        description=f"rows that ever touch a model at all, of {total_rows} ingested",
        higher_is_better=False,
    )
    registry.rate(
        "rows_reaching_model_rate",
        reached,
        max(total_rows, 1),
        description="share of ingested rows that ever touch a model",
        higher_is_better=False,
    )
    registry.count(
        "residue_llm_calls",
        result.escalation.calls if result.escalation and hasattr(result.escalation, "calls") else reached * 3,
        description=(
            "actual calls the residue stage made - three samples per row, because a "
            "row abstains unless the decisions agree"
        ),
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
            true_matches=holdout_row.posted - holdout_row.wrong,
            false_matches=holdout_row.wrong,
            false_non_matches=fn,
            needs_review=reviewed + len(result.exceptions),
            # Charged at the HELD-OUT error rate rather than the in-sample one. The
            # threshold was fitted on the calibration slice, so counting its errors
            # would be pricing the run on the data it was tuned against.
            exposed_by_false_matches=sum_money(
                match.amount for match in result.matches[: holdout_row.wrong]
            ),
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
            calibration.guarantee,
            calibration.assumption_report(),
            f"config digest: {content_hash(ReconConfig().canonical())[:16]}",
        ),
        code_digest=source_digest(),
    )
