"""``make eval`` - run the evaluation harness and write ``metrics.json``.

Two modes, chosen automatically:

* **pipeline** - once ``recon.pipeline`` exists (M6 onward), evaluate the real
  reconciliation against the ground-truth labels in the generated dataset.
* **selftest** - until then, exercise the harness itself on a fixed, obviously
  labelled fixture. This is not a product metric and the output says so in three
  places: the dataset name, a note in ``metrics.json``, and a banner on stdout.

The distinction matters. A harness that silently reported placeholder numbers as
though they were results would be exactly the dishonesty rule A.6 forbids.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

from core.costmodel import CostModel, OutcomeMix, price
from core.eval import (
    METRICS_PATH,
    Confusion,
    MetricRegistry,
    MetricsReport,
    append_history,
    bootstrap_ci,
    source_digest,
)
from core.ids import content_hash
from core.money import Money

ROOT = Path(__file__).resolve().parent.parent
SEED = 20260101

_SELFTEST_NOTE = (
    "SELFTEST: these figures come from a fixed fixture that exercises the harness, "
    "not from the reconciliation pipeline. They are not a claim about Triveni's "
    "accuracy. Real numbers land at M6 and replace this mode automatically."
)


def _pipeline_available() -> bool:
    try:
        import scripts.eval_pipeline  # noqa: F401
    except ImportError:
        return False
    return True


def selftest_report() -> MetricsReport:
    """Exercise every part of the harness on a deterministic fixture.

    The fixture is a plausible-shaped confusion matrix over 500 rows. Its only job
    is to prove the plumbing: rates compute, bootstrap intervals are stable across
    runs, the cost model prices an outcome mix, and the whole thing serialises to
    identical bytes twice.
    """
    rows = 500
    confusion = Confusion(tp=431, fp=6, fn=19, tn=44)

    registry = MetricRegistry()
    registry.rate(
        "match_rate",
        confusion.tp + confusion.fp,
        rows,
        description="share of source rows placed into an accepted match group",
        baseline=Decimal("0.720000"),
        baseline_label="exact-key rules only",
    )
    registry.rate(
        "precision",
        confusion.tp,
        confusion.tp + confusion.fp,
        description="of the matches we accepted, the share that were correct",
    )
    registry.rate(
        "recall",
        confusion.tp,
        confusion.tp + confusion.fn,
        description="of the matches that existed, the share we found",
    )
    registry.scalar(
        "f1",
        confusion.f1,
        description="harmonic mean of precision and recall",
    )
    registry.rate(
        "abstention_rate",
        confusion.fn,
        rows,
        description="share of rows Triveni declined to decide, with a reason",
        higher_is_better=False,
    )
    registry.rate(
        "llm_call_rate",
        31,
        rows,
        description="share of rows that required an LLM call at all",
        higher_is_better=False,
    )
    registry.money(
        "unexplained_amount",
        Money.from_rupees("18400"),
        description="rupees the waterfall could not attribute to a named component",
    )
    registry.count("rows_ingested", rows, description="source rows across all three ledgers")

    # A latency sample, to show the interval machinery works on a non-rate too.
    latencies = [12.0 + (i % 7) * 0.9 for i in range(rows)]
    registry.scalar(
        "stage_latency_ms_mean",
        sum(latencies) / len(latencies),
        unit="ms",
        interval=bootstrap_ci(latencies),
        description="mean per-row pipeline latency",
        higher_is_better=False,
    )

    mix = OutcomeMix(
        rows=rows,
        auto_posted=confusion.tp + confusion.fp,
        true_matches=confusion.tp,
        false_matches=confusion.fp,
        false_non_matches=confusion.fn,
        abstained=confusion.fn,
        needs_review=confusion.tn,
        exposed_by_false_matches=Money.from_rupees("64500"),
    )
    model = CostModel()
    breakdown = price(mix, model)
    registry.money("cost_of_being_wrong", breakdown.total, description="priced by core/costmodel.py")
    registry.money(
        "saved_vs_manual",
        breakdown.saved_vs_manual,
        description="versus reconciling the same rows by hand (negative = worse than manual)",
        higher_is_better=True,
    )
    # The number that actually governs where the conformal threshold belongs.
    budget = model.break_even_false_matches(
        rows=rows,
        exceptions=mix.human_touched,
        reviewed=mix.abstained + mix.needs_review,
        missed=mix.false_non_matches,
        avg_exposure=Money.from_rupees("10750"),
    )
    registry.scalar(
        "false_match_budget",
        budget,
        unit="n",
        description="false matches affordable before automation costs more than manual",
    )
    registry.count(
        "false_matches_realised",
        confusion.fp,
        description="compare against false_match_budget",
        higher_is_better=False,
    )

    return MetricsReport(
        dataset="harness-selftest",
        dataset_rows=rows,
        dataset_digest=content_hash({"fixture": "selftest", "rows": rows, "confusion": confusion.canonical()}),
        seed=SEED,
        metrics=tuple(registry.metrics),
        cost=breakdown,
        cost_model=model,
        confusion=confusion,
        notes=(
            _SELFTEST_NOTE,
            "The fixture's 6 false matches exceed the affordable budget, so "
            "saved_vs_manual is negative. That is the model working, not a bug: a "
            "false match costs ~15x a missed one, so automation only pays once the "
            "false-match rate is driven very low. This is the entire argument for "
            "the conformal threshold in core/conformal.py (M12).",
        ),
        code_digest=source_digest(),
    )


def build_report(dataset: str | None = None) -> MetricsReport:
    if _pipeline_available():
        from scripts.eval_pipeline import evaluate_pipeline

        return evaluate_pipeline(dataset=dataset, seed=SEED)
    return selftest_report()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="make eval", description="Run the evaluation harness.")
    parser.add_argument("--dataset", default=None, help="dataset name (default: the committed seed)")
    parser.add_argument("--out", type=Path, default=METRICS_PATH, help="where to write metrics.json")
    parser.add_argument("--milestone", default="", help="append a row to docs/metrics-history.md")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    report = build_report(args.dataset)
    path = report.write(args.out)

    if not args.quiet:
        if report.dataset == "harness-selftest":
            print("\033[33m" + "=" * 78)
            print("HARNESS SELFTEST - not a product metric.")
            print("The reconciliation pipeline does not exist yet (it lands at M6).")
            print("These numbers exercise the harness; they are not claims about accuracy.")
            print("=" * 78 + "\033[0m\n")
        print(report.render())
        print(f"\nwrote {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")

    if args.milestone:
        append_history(report, args.milestone)
        if not args.quiet:
            print(f"appended a row to docs/metrics-history.md for {args.milestone}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
