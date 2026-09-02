# ADR 0007 - Generated data, and the integrity rules that make its metrics mean anything

**Status:** accepted - 2026-09-02

**Context.** Without labels there is no precision, no recall, no conformal calibration
and no honest metric. We have no real merchant data and would not use it if we did, so
the dataset is synthetic - which means every metric Triveni reports is only as
trustworthy as the generator.

**Decision.** Generate the labels first and derive the data from them, never the other
way round, and enforce four integrity properties with tests.

1. **The labels balance.** Every truth group satisfies
   `gross + sum(components) == net_expected`, exactly, in integer paise. The generator
   refuses to emit a dataset where this fails. A truth that does not balance would
   grade a correct matcher as broken.
2. **The sources do not leak the answer.** Underscore-prefixed generator bookkeeping
   (`_settles_on`, `_bank_name`) is stripped before writing: a real gateway response
   does not tell you which settlement a payment landed in, and a matcher that read it
   would be scoring itself on a problem nobody has.
3. **Row ids are opaque.** Bank rows ship as `TR` + 10 hex, derived by hash from the
   internal id. Semantic ids like `unknown-0` or `corrupt-1` would let a trivial
   "matcher" read the prefix and score perfectly.
4. **`recon/` cannot see the labels.** Asserted structurally by a test that greps the
   matching packages for any reference to the ground truth.

**Quotas, not coin flips.** Anomalies are allocated by quota
(`max(1, round(rate * slots))`, spread by a deterministic stride) rather than by
independent Bernoulli draws. The first implementation used coin flips and, with 31
settlement groups, a 3% rate fires 0.9 times in expectation - so six of the sixteen
exception types were absent from the committed seed. That would have meant grading the
pipeline on a subset of the problem and shipping a demo that never shows a short-pay.
Quotas realise the documented rates exactly instead of in expectation, which is what a
fixture needs; the rates in `DatasetSpec` still describe the data. Contradictory pairs
(short *and* over, missing *and* split) are resolved after allocation.

**Shape.** The committed seed is 520 rows over 32 settlement groups, 17 of them
many-to-one with up to 14 payments collapsing into a single bank credit; the full set
is 5,481 rows over 158 groups with up to 55. That netting is the whole reason
`recon/subsetsum.py` and the global assignment solver exist - without it the matcher
would only ever need 1:1 lookup and the solver would be decoration.
