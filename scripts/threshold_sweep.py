"""Where should the Fellegi-Sunter link threshold go?

Not "wherever F1 is highest". F1 assumes a false match and a missed match cost the
same, and they do not: a false match silently corrupts the books and is found weeks
later, a missed match sits in a queue for three minutes. `core/costmodel.py` prices
that asymmetry, so the threshold can be chosen by the number that actually matters.

    python -m scripts.threshold_sweep

This is a heuristic, and it is labelled as one. It picks the point on *this* dataset
that minimises rupee cost, which is an in-sample choice with no guarantee attached.
M12 replaces it with split-conformal calibration, which gives a distribution-free
bound on the false-match rate instead of a number that happened to work here. The
sweep stays afterwards because it is what makes the alpha slider legible: it is the
cost curve the operating point travels along.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core.costmodel import CostModel, OutcomeMix, price
from core.money import format_inr, sum_money
from recon.pipeline import ReconConfig, reconcile
from scripts.blocking_report import true_pairs_for
from scripts.eval_pipeline import cross_source_pairs

ROOT = Path(__file__).resolve().parent.parent


def sweep(directory: Path, thresholds: list[float]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for threshold in thresholds:
        config = ReconConfig(link_upper_bits=threshold)
        result = reconcile(directory=directory, config=config)
        truth = true_pairs_for(directory, list(result.rows.values()))

        by_txn = {r.txn_id: r for r in result.rows.values()}
        predicted: set[tuple[str, str]] = set()
        for match in result.matches:
            predicted |= cross_source_pairs(
                list(match.gateway_ids), list(match.bank_ids), list(match.ledger_ids)
            )

        tp = len(predicted & truth)
        fp = len(predicted - truth)
        fn = len(truth - predicted)

        exposed = sum_money(
            (
                by_txn[a].amount
                for a, _b in sorted(predicted - truth)
                if a in by_txn
            ),
            "INR",
        )
        breakdown = price(
            OutcomeMix(
                rows=len(result.rows),
                auto_posted=len(result.matches),
                true_matches=tp,
                false_matches=fp,
                false_non_matches=fn,
                needs_review=len(result.exceptions),
                exposed_by_false_matches=exposed,
            ),
            CostModel(),
        )
        rows.append(
            {
                "threshold": threshold,
                "matches": len(result.matches),
                "tp": tp,
                "fp": fp,
                "fn": fn,
                "precision": tp / max(tp + fp, 1),
                "recall": tp / max(tp + fn, 1),
                "cost": breakdown.total,
                "saved": breakdown.saved_vs_manual,
            }
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="threshold-sweep")
    parser.add_argument("--spec", default="seed")
    args = parser.parse_args(argv)
    directory = ROOT / "data" / ("seed" if args.spec == "seed" else f"generated/{args.spec}")

    thresholds = [0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 16.0, 20.0, 25.0, 30.0, 40.0]
    rows = sweep(directory, thresholds)

    print(f"dataset: {args.spec}\n")
    print(
        f"{'bits':>6} {'matches':>8} {'TP':>5} {'FP':>4} {'FN':>5} "
        f"{'precision':>10} {'recall':>8} {'cost':>14} {'saved':>14}"
    )
    print("-" * 84)
    best = min(rows, key=lambda r: r["cost"].paise)  # type: ignore[union-attr]
    for row in rows:
        mark = "  <- lowest cost" if row is best else ""
        print(
            f"{row['threshold']:>6.1f} {row['matches']:>8} {row['tp']:>5} {row['fp']:>4} "
            f"{row['fn']:>5} {row['precision']:>10.4f} {row['recall']:>8.4f} "
            f"{format_inr(row['cost']):>14} {format_inr(row['saved']):>14}{mark}"  # type: ignore[arg-type]
        )

    best_f1 = max(
        rows,
        key=lambda r: 2 * r["precision"] * r["recall"] / max(r["precision"] + r["recall"], 1e-9),  # type: ignore[operator]
    )
    print(
        f"\nlowest rupee cost at {best['threshold']:.1f} bits; "
        f"highest F1 at {best_f1['threshold']:.1f} bits"
    )
    if best["threshold"] != best_f1["threshold"]:
        print(
            "These disagree, which is the whole point: F1 treats a false match and a\n"
            "missed match as equally bad, and the cost model does not."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
