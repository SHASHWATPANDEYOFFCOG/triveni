"""Pair completeness and reduction ratio, per blocking strategy.

The two standard blocking metrics, measured against ground truth - which is why this
lives in `scripts/` and not in `recon/`. Prints the table that goes in the README.

    python -m scripts.blocking_report              # committed seed
    python -m scripts.blocking_report --spec full  # the 5,000-row dataset
    python -m scripts.blocking_report --markdown   # README-ready

Pair completeness is the number that matters most in the whole pipeline: anything
blocking discards, no later stage can recover, so PC is a hard ceiling on final recall.
Reduction ratio is what pays for the stages that follow.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

from ingest.adapters.csv_bank import read_bank_statement, read_ledger
from ingest.adapters.fixtures import read_gateway
from recon.blocking import (
    CandidateSet,
    Pair,
    generate_candidates,
    possible_cross_source_pairs,
    relevant_rows,
)
from recon.pipeline import canonicalise
from scripts.truth import true_pairs_from_group

ROOT = Path(__file__).resolve().parent.parent


def load_rows(directory: Path):
    sources = [
        read_gateway(directory),
        read_bank_statement(directory=directory),
        read_ledger(directory=directory),
    ]
    rows = [row for source in sources for row in source.rows]
    canonical, _ = canonicalise(rows)
    return canonical


def true_pairs_for(directory: Path, rows) -> set[Pair]:
    """True cross-source pairs, expressed in internal txn ids.

    The ground truth speaks in external ids (``pay_...``, ``INV-...``, ``TR...``), so
    they are translated here rather than anywhere the matcher can see.
    """
    truth = json.loads((directory / "ground_truth.json").read_text(encoding="utf-8"))
    by_external: dict[str, str] = {}
    for row in rows:
        by_external.setdefault(row.external_id, row.txn_id)

    pairs: set[Pair] = set()
    for group in truth["groups"]:
        for a, b in true_pairs_from_group(group):
            if a in by_external and b in by_external:
                left, right = by_external[a], by_external[b]
                pairs.add((left, right) if left < right else (right, left))
    return pairs


def pair_completeness(candidates: set[Pair], truth: set[Pair]) -> Decimal:
    if not truth:
        return Decimal(1)
    return Decimal(len(candidates & truth)) / Decimal(len(truth))


def build(directory: Path) -> tuple[CandidateSet, set[Pair], int, list]:
    rows = relevant_rows(load_rows(directory))
    truth = true_pairs_for(directory, rows)
    candidates = generate_candidates(rows)
    for report in candidates.reports:
        pairs = (
            candidates.pairs
            if report.name == "union (all)"
            else candidates.by_strategy[report.name]
        )
        object.__setattr__(report, "pair_completeness", pair_completeness(pairs, truth))
    return candidates, truth, possible_cross_source_pairs(rows), rows


def render_table(candidates: CandidateSet, truth: set[Pair], possible: int, markdown: bool) -> str:
    lines: list[str] = []
    if markdown:
        lines.append("| strategy | candidate pairs | reduction ratio | pair completeness | what it catches |")
        lines.append("|---|---:|---:|---:|---|")
        for report in candidates.reports:
            pc = "-" if report.pair_completeness is None else f"{report.pair_completeness:.4f}"
            bold = "**" if report.name == "union (all)" else ""
            lines.append(
                f"| {bold}{report.name}{bold} | {report.candidate_pairs:,} | "
                f"{report.reduction_ratio:.6f} | {bold}{pc}{bold} | {report.description} |"
            )
    else:
        lines.append(f"{'strategy':22} {'pairs':>10}  {'RR':>10}  {'PC':>8}  description")
        lines.append("-" * 100)
        for report in candidates.reports:
            pc = "-" if report.pair_completeness is None else f"{report.pair_completeness:.4f}"
            lines.append(
                f"{report.name:22} {report.candidate_pairs:>10,}  "
                f"{report.reduction_ratio:>10.6f}  {pc:>8}  {report.description}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="blocking-report")
    parser.add_argument("--spec", default="seed", help="seed, or a name under data/generated")
    parser.add_argument("--markdown", action="store_true")
    parser.add_argument("--threshold", type=float, default=0.99, help="required pair completeness")
    args = parser.parse_args(argv)

    directory = ROOT / "data" / ("seed" if args.spec == "seed" else f"generated/{args.spec}")
    if not directory.exists():
        print(f"no dataset at {directory}; run `make data --spec {args.spec}` first")
        return 2

    candidates, truth, possible, rows = build(directory)

    print(f"dataset: {args.spec}  ({len(rows)} blockable rows)")
    print(f"cross-source pairs a brute-force matcher would compare: {possible:,}")
    print(f"true cross-source pairs in the ground truth: {len(truth):,}\n")
    print(render_table(candidates, truth, possible, args.markdown))

    union = next(r for r in candidates.reports if r.name == "union (all)")
    assert union.pair_completeness is not None
    missed = len(truth - candidates.pairs)
    print(
        f"\nunion keeps {union.pair_completeness:.4%} of true pairs while discarding "
        f"{union.reduction_ratio:.4%} of all comparisons"
    )
    print(f"{missed} true pair(s) lost to blocking - an absolute ceiling on final recall")

    if union.pair_completeness < Decimal(str(args.threshold)):
        print(
            f"\n\033[31mFAIL: pair completeness {union.pair_completeness:.4f} is below "
            f"the required {args.threshold}\033[0m"
        )
        return 1
    print(f"\n\033[32mPASS: pair completeness >= {args.threshold}\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
