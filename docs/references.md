# References

What we took from each, specifically. The rule for this file: **do not cite a paper you
have not read, and never attribute a number to a paper you have not read the number in.**
Where the entry describes an idea rather than a result, it says so — several of these
informed a design decision without any figure from them appearing anywhere in this repo.

## Record linkage

**Fellegi & Sunter, *A Theory for Record Linkage* (JASA, 1969).**
The formulation Stage 3 implements: per-field agreement levels, m and u probabilities,
and a total match weight as the sum of per-field log-likelihood ratios. We implement it
directly (~200 lines, `recon/fellegi_sunter.py`) with EM-estimated parameters, because
the per-field weight breakdown is what makes a match explainable in the triage UI — a
score with no decomposition is a number a finance team cannot act on.

**Splink (Linacre, Bond et al., *IJPDS*, 2022) — `moj-analytical-services.github.io/splink`.**
The modern scalable implementation of the above. Referenced as the design we are following
rather than a dependency: we own the implementation so the weights, the blocking and the
scoring stay auditable end to end. No figure from Splink appears here.

## Blocking

**Thirumuruganathan et al., *Deep Learning for Blocking in Entity Matching* (VLDB, 2021),
and the VLDB experimental analyses of pre-trained embeddings for entity resolution.**
The design point we took: dense blocking substantially improves recall over rule-only
blocking at comparable cost, and blocking should be evaluated with **pair completeness**
and **reduction ratio** rather than by downstream match rate alone. `recon/blocking.py`
reports both per scheme. We do *not* claim their measured improvements — our dense
blocker is a local hashed character n-gram embedding, not a learned one (see
`docs/limitations.md`), so their numbers do not transfer.

**Towards Universal Dense Blocking for Entity Resolution (arXiv 2404.14831).**
Read for the framing of blocking as a recall-preserving filter with an explicit budget.

## LLMs for entity matching

**Peeters & Bizer, *Entity Matching using Large Language Models* (arXiv 2310.11244);
*AnyMatch* (arXiv 2409.04073); *Match, Compare, or Select?* (COLING 2025).**
Collectively the reason the LLM is the **last** stage on the residue rather than the
matcher. These show LLMs are competitive at pairwise matching — but pairwise is the wrong
frame when the true problem is a global assignment with a no-double-booking constraint,
and a model that is competitive per-pair still cannot enforce that. Hence: solver first,
model only on what the solver could not resolve, and even then it classifies rather than
matches.

## Conformal prediction

**Vovk, Gammerman & Shafer, *Algorithmic Learning in a Random World*.**
The foundation: distribution-free validity under exchangeability.

**Angelopoulos & Bates, *A Gentle Introduction to Conformal Prediction* (arXiv 2107.07511).**
The split-conformal recipe `core/conformal.py` follows, and the source of the framing we
use in the UI — that the guarantee is marginal, not conditional.

**Angelopoulos et al., *Conformal Risk Control*.**
The finite-sample correction `α − (B − α)/n` that turns an in-sample observation into a
bound, and the reason the method correctly refuses to promise anything at small n. This
is the specific result the auto-post threshold rests on.

## Selective prediction and abstention

**Chow's rule; the learning-to-defer literature; recent conformal-abstention work for LLMs.**
The idea that abstention is a *modelled option with a price* rather than a failure. In
Triveni that price is explicit: `core/costmodel.py` charges an abstention the same human
minutes as a missed match, which is what makes "route to a human" comparable against
"post it" in rupees rather than in vibes.

## LLM uncertainty

**Farquhar, Kossen, Kuhn & Gal, *Detecting hallucinations in large language models using
semantic entropy*, Nature 630 (2024).**
The idea we applied: measure agreement over *meanings* rather than token strings. Our
version is deliberately cruder — `core/llm.py` samples the residue prompt k times and
measures agreement on the **decision** (the classification), not on semantic clusters of
the text. A model that says "timing difference" and "settlement timing" has agreed about
what to do. We claim the spirit, not the method.

## Assignment and flow

**The assignment problem — Hungarian / Jonker–Volgenant, as in
`scipy.optimize.linear_sum_assignment`.**
Stage 4's 1:1 layer, with a dummy "no-match" column priced at the conformal threshold so
declining to match is a modelled option with a cost rather than an afterthought.

**Min-cost flow, including the almost-linear-time result (Chen et al., JACM 2025).**
Cited as the reason global assignment is the *correct formulation* for this problem — not
as a performance claim. We have measured nothing against it and our solver is CP-SAT plus
Hungarian, not a min-cost-flow implementation.

**Many-to-Many Matching via Sparsity Controlled Optimal Transport (arXiv 2503.24204).**
Read while designing the many-to-one layer. We went with exact subset-sum plus CP-SAT
instead, because a settlement either sums to the credit within tolerance or it does not,
and an exact answer with a proof of optimality is worth more here than a soft one.

## Conformal prediction for time series

**Xu & Xie, *Conformal prediction for time series* (EnbPI); ensemble conformalised quantile
regression; Zaffran et al., *Adaptive conformal predictions for time series* (ICML, 2022).**
Two things we took. First, that exchangeability is strained hardest in time series, so
calibration should use a **recent** window rather than all of history. Second, and this
one cost us a bug: residuals must be held out and must be kept **per horizon step** —
pooling one-step residuals across a 14-day horizon under-covers badly at the far end, and
ours realised 77.4% against a 90% nominal until it was split out.

## Time-series foundation models

**The zero-shot TSFM line — Chronos, TimesFM, Moirai, TabPFN-TS — and GIFT-Eval-style
benchmarks.**
Treated as an optional, benchmarked component and **feature-flagged off by default**, so a
cold `make demo` never downloads a model. Our boosted baseline wins on this data at MASE
0.546; we make no claim about how a TSFM would compare, because we did not run one.

## Tamper-evident logging

**Certificate Transparency — RFC 6962 and RFC 9162.**
Implemented directly in `core/audit/merkle.py`: the 0x00/0x01 domain separation, the
`MTH` definition, inclusion paths and consistency proofs, and the RFC 9162 verifier
pseudocode transcribed closely enough that a reader can diff it against the spec. The
argument we took from CT is the one in the README: a hash chain cannot prove append-only,
and consistency proofs are the construction that can.

We do **not** implement the rest of CT — no log operator, no monitors, no gossip. The
threat model says what that leaves open.

## Grounded analytics

**The BIRD text-to-SQL benchmark line of work on execution-guided generation.**
The idea that generated SQL should be validated by *executing* it rather than by
inspecting it. Triveni goes further in one direction and less far in another: the model
never generates SQL at all (it selects from ten hand-written templates), and the check is
not just that the query executed but that **every numeral in the answer appears in the
result set**. No benchmark number from this line is claimed.

## Indian payments domain

The MDR structure by method (zero on UPI and RuPay debit), 18% GST charged on the fee
rather than the sale, TDS under section 194-O at 0.1%, T+2 settlement, rolling reserves,
and the 2nd/4th-Saturday bank calendar are all modelled from general domain knowledge of
Indian payments rather than from a cited source. They are encoded as **editable data and
one-line testable functions** (`data/gen.py::MDR_BPS`, `core/clock.py`,
`data/calendars/holidays_in.json`) precisely so a merchant who knows better can correct
them without touching the engine — and `docs/limitations.md` says the holiday calendar
beyond the three gazetted national holidays is operator-supplied, because asserting a
fuller list as fact would be inventing precision we do not have.
