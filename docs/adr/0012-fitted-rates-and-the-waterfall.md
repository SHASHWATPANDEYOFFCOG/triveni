# ADR 0012 - Fit the rates, report what cannot be fitted

**Status:** accepted - 2026-09-02

**Context.** A settlement is short and the merchant wants to know why. "Fees" is not
an answer. The answer is a waterfall from gross to the exact amount the bank credited,
with each deduction named and each rate checkable. `recon/fees.py` holds a rate card,
but a card is what the *contract* says; the settlement is what actually happened.

**Decision.** Fit the effective rates from the batch, and be explicit about which of
them the data can actually support.

**Result on the seed:** 16 of 16 waterfalls balance to Rs 0, rates fitted on 12
fee-consistent settlements at **R² 0.919**, and overall precision rose from 0.9169 to
**0.9773** with recall 0.8263.

## Three things the measurement forced

**1. Exclude settlements whose gap cannot be fees.** A settlement's withheld amount is
not only fees: a 5% rolling reserve, a chargeback hold and a genuine short-pay all land
in the same number, and least squares explains them by inflating whichever MDR
coefficient helps. Fitting on everything gave a `card_debit` rate of **2,524 bps against
a contracted 90**, and **3,871 bps on RuPay debit - a method zero-rated by regulation** -
at R² 0.59. Restricting the fit to settlements whose withheld share is within the
plausible fee range took R² to 0.92 and the unexplained residual from Rs 27,557 to
Rs 1,009. This is a stated rule about the response variable's plausible range, not a
selection on whether points happen to fit; excluded settlements are still decomposed,
they simply do not get a vote on what the fee rates are.

**2. Robust loss, and report the non-robust one beside it.** Huber loss stops a handful
of reserve-bearing settlements dominating. The plain NNLS fit is kept in the output
because the gap between them is the argument for using a robust loss at all, and a
reader should be able to see it rather than take it on trust.

**3. Refuse to report a rate fitted from too little volume.** EMI is 1% of volume on
the seed and fits to 2,813 bps against a contracted 300; international cards are 0.6%
and fit to 4,484 against 430. Those are not estimates, they are noise wearing an
estimate's clothes. `FittedRates.identifiable()` gates on volume share, `as_rate_card()`
keeps the contracted rate for anything below it, and the comparison table prints "too
thin to fit" rather than a number. Non-negativity is enforced throughout: a negative
fee is not a rate, it is evidence the model is wrong, and forbidding it makes that
surface as a residual instead of being absorbed by a nonsense coefficient.

## Two corrections carried in from Stage 4

**The objective was misspecified, and an accident hid it.** The settlement calendar and
the amount residual were priced comparably, but T+2 is contractual, not statistical.
The single-model solver was under-budgeted, stayed near the date-based hint, and scored
0.9169; decomposing the problem gave it more effective search, it moved away from the
hint toward a better objective value, and precision fell to 0.83. **The hint being
better than the optimum is not a solver problem - it is the objective being wrong.**
Pricing a day of calendar drift as dominant evidence took precision to 0.9838.

**Solver optimality is not correctness.** Stage 4 briefly claimed 0.95 confidence when
CP-SAT proved optimality, which pushed settlement attributions over the auto-post
threshold - and auto-post precision fell from 1.000 to 0.977, meaning 2.3% of wrong
attributions were reaching the books because a solver said it had finished. Optimality
means "best under my objective". Netting attributions now carry a confidence below the
auto-post threshold unconditionally: they are proposals until M12's conformal
calibration provides a basis for trusting them, and auto-post precision is back at
**1.000**.

**Nothing in this decomposition touches an LLM**, and a test asserts it. Deciding how
much a fee is comes from closed-form arithmetic and a least-squares fit; a model
guessing rupees would be a disqualifier.
