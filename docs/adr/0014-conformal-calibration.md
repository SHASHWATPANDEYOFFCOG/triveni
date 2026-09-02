# ADR 0014 - A guarantee instead of a hunch

**Status:** accepted - 2026-09-03

**Context.** Until M12 the auto-post threshold was `0.90`, hard-coded. That number
meant nothing: it was not a probability of anything, it would not transfer to next
month's data, and when it was wrong nobody could say by how much. Almost every
reconciliation tool ships one like it.

**Decision.** Replace it with **split conformal calibration under conformal risk
control** (Angelopoulos, Bates et al.). Hold out a labelled calibration slice, sweep
the threshold, and take the most permissive one whose empirical error clears a
*corrected* budget:

    q_hat = inf { t : R_hat(t) <= alpha - (B - alpha) / n }

The correction is what turns an in-sample observation into a bound. Without it you have
measured error on the data you tuned on. With it, a small calibration set correctly
refuses to promise anything - at n=40 and alpha=0.05 the correction exceeds alpha, and
`available=False` is the right answer rather than a weak guarantee dressed as a strong
one.

**Measured on the committed seed.**

| | before (hard-coded 0.90) | after (calibrated at alpha=1%) |
|---|---:|---:|
| threshold | 0.90 (chosen) | **0.9548 (fitted)** |
| auto-post coverage | 41.1% | **72.8%** |
| auto-post precision | 100% | **100%** |
| realised error, held-out | not measured | **0.00%** against a 1.00% bound |
| cost of the run | Rs 12,530 | **Rs 8,850**, saving Rs 5,230 vs manual |

## The subtlety this milestone turned on

On a single split, **alpha=2% breaches** - 2.64% realised against a 2.00% promise. The
first instinct is to treat that as a bug. It is not, and neither is it something to
hide: conformal risk control bounds the error **in expectation over the draw of the
calibration set**, so an individual split can and does exceed it.

So both are reported. `coverage_table` shows the single split a user actually sees,
breach and all. `validate_guarantee` re-splits 40 times and reports the mean, which is
the quantity the theorem is about - and every bound holds there (1.24% mean at
alpha=2%). It also reports the **breach rate**, because "the mean holds and 2% of runs
breach" and "the mean holds and 33% of runs breach" are materially different products,
and only the second number tells an operator which one they have.

## Three fixes that mattered

**A constant confidence made the slider meaningless.** With every claim scoring 1.00 or
0.85, the threshold had two places to sit and the cost curve was flat across the entire
alpha range. Stage 4 now derives confidence continuously from evidence available at
that stage - how many payments landed on the date the calendar predicted, and how close
the withheld amount is to what the rate card expects. Neither is the label.

**A linear alpha grid missed the entire trade.** Starting at 1/(2*steps) = 2.4%, every
point reported the same threshold, coverage and cost. It looked like a working curve
and was not. On a geometric grid the real structure appears: below alpha=1.2% the
threshold sits at 0.9548 admitting 70.3% of claims with **zero** held-out errors at
Rs 1,800; above alpha=1.6% it drops to 0.82, admits everything, and costs Rs 7,600.
That step is the operating decision, and it was invisible before.

**An id-space mismatch silently disabled the whole feature.** Claims come out of the
pipeline in internal txn ids; ground truth speaks external ids. Comparing them directly
marked every claim wrong, so no threshold could clear the budget, the calibration
reported itself unavailable, and the code fell back to the hard-coded 0.90 it existed
to replace - while printing a plausible-looking threshold. A feature that fails back to
the thing it replaces, quietly, is worse than one that fails loudly.

**The assumption is in the output, not a footnote.** Conformal prediction needs
*exchangeability*, and reconciliation data violates it routinely: a gateway changes its
settlement schedule and the timing distribution shifts; a new payment method has a fee
structure the model has never seen; month-end is not exchangeable with mid-month; and
calibration labels exist precisely because a human looked at those rows, which is not a
random sample of anything. `Calibration.assumption_report()` says all of that, and it
is carried into `metrics.json` alongside the guarantee sentence.
