# ADR 0010 - Fellegi-Sunter fitted by EM, and why deduplication had to come first

**Status:** accepted - 2026-09-02

**Context.** Blocking says which pairs are worth comparing. Something has to say how
much each comparison is worth, and - for a finance team - why.

**Decision.** Implement Fellegi-Sunter (JASA 1969) directly, ~380 lines, with `m` and
`u` fitted by expectation-maximisation over the candidate pairs and no labels. Each
field contributes `log2(m/u)` bits; the total is their sum. Splink (Linacre et al.,
IJPDS 2022) is the modern scalable implementation of the same theory - we own ours so
the per-field weight breakdown is ours end to end, because that breakdown is what the
triage UI renders and what makes a match arguable rather than merely asserted.

Two thresholds, not one. Above the upper, link; below the lower, reject; between them
is the **clerical review** region the method is named for, which becomes a typed
exception carrying its full weight breakdown. Collapsing that to a single threshold
would throw away the model's most useful output: its admission that it does not know.

**Three things measurement caught that assumption would not have.**

1. **EM was being fitted on the residue.** Stage 1 removes the easy true matches, so
   the leftovers are a badly biased sample. The model learned that landing *in* the
   expected settlement window was evidence **against** a match, because among the
   residue it genuinely was. EM is now fitted on the full candidate set and applied to
   the open subset. Only reading the fitted weight table revealed this - the model
   converged happily either way.

2. **Date comparison must be relation-aware.** A payment and its invoice are both
   dated on the day of the sale; only a payment-to-bank-credit link is T+2. Projecting
   T+2 across every relation put every true 1:1 pair two days from "expected", and EM
   dutifully inverted the date weights.

3. **Deduplication had to come first.** Stage 3's five false positives all scored
   **+47.9 bits** - no threshold could exclude them, and a cost sweep across 0 to 40
   bits moved nothing. They were injected duplicate payments being linked to the
   invoices their originals should have owned, and the model was not wrong about the
   evidence: the rows are byte-identical. The fix was a deduplication pass keyed on
   (source, reference, amount, timestamp) that sets copies aside as typed `duplicate`
   exceptions. Precision returned to 1.000, Stage 1's order-id matches rose from 228 to
   238, and the `duplicate` exception type fired for the first time.

**Assumption, stated.** Conditional independence given match status is not exactly true
here - amount and reference agreement are correlated, both being consequences of being
the same payment - and the effect is over-confidence at the extremes. This is why M12's
conformal calibration maps scores to a *guaranteed* error rate rather than trusting
these probabilities directly. Using a model whose assumptions you know to be
approximate is fine; pretending otherwise is not.

**Result on the seed:** match rate 0.9084 -> 0.9495, recall 0.3324 -> 0.3475,
precision 1.000 held. The remaining recall gap is almost entirely the many-to-one
netting relation, which is exactly what Stage 4's subset solver exists for.
