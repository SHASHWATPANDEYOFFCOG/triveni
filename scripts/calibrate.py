"""Calibrate the auto-post threshold, and measure whether the bound actually held.

    python -m scripts.calibrate                 # coverage table + the alpha curve
    python -m scripts.calibrate --alpha 0.05    # one operating point in detail
    python -m scripts.calibrate --spec full     # on the 5,481-row dataset

Lives in `scripts/` because it needs the labels, and `core/conformal.py` is
deliberately label-free machinery that is handed observations. The split between them
is the same one as everywhere else in this repo: the thing being measured cannot see
the answers.

What this prints is the honesty table. For each alpha it shows what was **promised**
and what was **realised on a held-out split** - because a 5% bound that realises 9% is
a lie, and printing both side by side is the only way anyone finds out.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

from core.conformal import (
    Observation,
    alpha_curve,
    best_operating_point,
    calibrate,
    coverage_table,
    evaluate_on,
    split,
    validate_guarantee,
)
from core.costmodel import CostModel
from core.money import Money, format_inr
from recon.pipeline import reconcile
from scripts.blocking_report import true_pairs_for

ROOT = Path(__file__).resolve().parent.parent


def observations_for(directory: Path) -> list[Observation]:
    """Every pairwise claim the pipeline made, scored and labelled.

    The unit is a *pair*, not a match group, because a pair is what can be right or
    wrong: a settlement group that attributes nineteen payments correctly and one
    incorrectly is not "a wrong match", it is nineteen right claims and one wrong one,
    and a guarantee about posted claims has to be measured on claims.
    """
    result = reconcile(directory=directory)
    truth = true_pairs_for(directory, list(result.rows.values()))

    observations: list[Observation] = []
    for match in result.matches:
        for left, right in sorted(match.claimed_pairs()):
            observations.append(
                Observation(
                    identifier=f"{left}|{right}",
                    confidence=match.confidence,
                    correct=(left, right) in truth,
                    amount_paise=match.amount.paise,
                )
            )
    return sorted(observations, key=lambda o: o.identifier)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="calibrate")
    parser.add_argument("--spec", default="seed")
    parser.add_argument("--alpha", type=str, default="")
    parser.add_argument("--curve", action="store_true", help="print the full alpha curve")
    args = parser.parse_args(argv)

    directory = ROOT / "data" / ("seed" if args.spec == "seed" else f"generated/{args.spec}")
    observations = observations_for(directory)
    model = CostModel()

    print(f"dataset: {args.spec}   {len(observations)} labelled claim(s)\n")

    if args.alpha:
        alpha = Decimal(args.alpha)
        calibration_set, test_set = split(observations)
        calibrated = calibrate(calibration_set, alpha)
        row = evaluate_on(calibrated, test_set)
        print(calibrated.guarantee)
        print()
        print(f"  held-out: {row.posted} posted, {row.wrong} wrong "
              f"(realised {row.realised:.2%} against a nominal {row.nominal:.2%}), "
              f"{row.reviewed} routed to a human")
        print(f"  bound {'HELD' if row.holds else 'WAS BREACHED'}")
        print()
        print("  " + calibrated.assumption_report().replace(". ", ".\n  "))
        return 0

    rows, table = coverage_table(observations)
    print(table)

    print("\n")
    _validation, validation_table = validate_guarantee(observations)
    print(validation_table)

    print("\n\nWhat this costs, and where a merchant would actually set it:\n")
    false_post = model.cost_per_false_match(Money.from_rupees("10000")).paise
    review = model.cost_per_review().paise
    points = alpha_curve(
        observations,
        cost_per_false_post_paise=false_post,
        cost_per_review_paise=review,
    )
    best = best_operating_point(points)

    print(
        f"  {'alpha':>7}  {'threshold':>9}  {'coverage':>9}  {'realised':>9}  "
        f"{'wrong':>6}  {'reviewed':>9}  {'cost':>13}"
    )
    print("  " + "-" * 72)
    for point in points:
        if not point["available"]:
            continue
        mark = "  <- lowest cost" if best is not None and point is best else ""
        print(
            f"  {point['alpha']:>7}  {point['threshold']:>9}  {point['coverage']:>9.1%}  "
            f"{point['realised_error']:>9.2%}  {point['wrong']:>6}  "
            f"{point['reviewed']:>9}  {format_inr(Money(int(point['cost_paise']))):>13}{mark}"
        )

    if best is None:
        print(
            "\n  No alpha produced a usable guarantee on this dataset. That is the "
            "honest answer, not a failure to report one."
        )
    else:
        print(
            f"\n  A merchant would ship alpha={best['alpha']}: it posts "
            f"{best['coverage']:.1%} of claims unsupervised, realises "
            f"{best['realised_error']:.2%} error on held-out data, and costs "
            f"{format_inr(Money(int(best['cost_paise'])))}."
        )

    unavailable = [p for p in points if not p["available"]]
    if unavailable:
        print(
            f"\n  {len(unavailable)} of {len(points)} alphas admit nothing: the "
            f"finite-sample correction exceeds them at this calibration size. The "
            f"method refusing to promise anything is the correct behaviour."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
