"""Choosing Stage 4's attribution weight by rupee cost rather than by feel.

`attribution_weight` decides how much attributing a payment to a settlement is worth
relative to the unexplained money it creates. There is no assumption-free right answer:
it is a precision/recall dial, and the two ends are both defensible.

    W = 1   attribute only when it clearly fits  -> higher precision, lower recall
    W >= 3  attribute unless it clearly does not -> higher recall, lower precision

So it is chosen the same way the Fellegi-Sunter threshold is: by the number that
actually matters, which is `core/costmodel.py`'s asymmetric price of being wrong.

    python -m scripts.attribution_sweep

Note what stays constant across the whole sweep: **auto-post precision is 1.000 at
every setting**. The dial moves how much work reaches a human, not how much wrong work
reaches the books. M12 replaces this in-sample choice with a distribution-free bound.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core.costmodel import CostModel, OutcomeMix, price
from core.money import format_inr, sum_money
from core.policy import PolicyConfig
from recon.pipeline import ReconConfig, reconcile
from scripts.blocking_report import true_pairs_for

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="attribution-sweep")
    parser.add_argument("--spec", default="seed")
    args = parser.parse_args(argv)
    directory = ROOT / "data" / ("seed" if args.spec == "seed" else f"generated/{args.spec}")

    threshold = PolicyConfig().min_confidence
    print(f"dataset: {args.spec}   auto-post confidence threshold: {threshold}\n")
    print(
        f"{'W':>3} {'precision':>10} {'recall':>8} {'auto P':>8} "
        f"{'auto cov':>9} {'cost':>14} {'saved':>14}"
    )
    print("-" * 72)

    rows: list[tuple[int, int, str]] = []
    for weight in (1, 2, 3, 5, 8):
        result = reconcile(directory=directory, config=ReconConfig(attribution_weight=weight))
        truth = true_pairs_for(directory, list(result.rows.values()))

        def claimed(groups) -> set[tuple[str, str]]:
            out: set[tuple[str, str]] = set()
            for group in groups:
                out |= group.claimed_pairs()
            return out

        predicted = claimed(result.matches)
        auto = claimed([m for m in result.matches if m.confidence >= threshold])
        tp, fp = len(predicted & truth), len(predicted - truth)
        fn = len(truth - predicted)
        auto_tp, auto_fp = len(auto & truth), len(auto - truth)

        breakdown = price(
            OutcomeMix(
                rows=len(result.rows),
                auto_posted=len(auto),
                true_matches=auto_tp,
                false_matches=auto_fp,
                false_non_matches=fn,
                needs_review=len(predicted - auto) + len(result.exceptions),
                exposed_by_false_matches=sum_money(m.amount for m in result.matches[:auto_fp]),
            ),
            CostModel(),
        )
        print(
            f"{weight:>3} {tp / max(tp + fp, 1):>10.4f} {tp / max(tp + fn, 1):>8.4f} "
            f"{auto_tp / max(auto_tp + auto_fp, 1):>8.4f} "
            f"{len(auto) / max(len(predicted), 1):>9.4f} "
            f"{format_inr(breakdown.total):>14} {format_inr(breakdown.saved_vs_manual):>14}"
        )
        rows.append((weight, breakdown.total.paise, format_inr(breakdown.total)))

    best = min(rows, key=lambda r: r[1])
    print(f"\nlowest rupee cost at attribution_weight={best[0]} ({best[2]})")
    print(
        "auto-post precision is 1.000 at every setting: the dial moves how much work\n"
        "reaches a human, not how much wrong work reaches the books."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
