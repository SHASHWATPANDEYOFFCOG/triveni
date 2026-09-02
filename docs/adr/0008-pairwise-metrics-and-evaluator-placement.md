# ADR 0008 - Pairwise metrics, and where the evaluator is allowed to live

**Status:** accepted - 2026-09-02

**Context.** M6's first evaluation reported **100% recall**. It was nonsense: it
divided a count of 228 invoice-to-payment match groups by a count of 17 settlement
groups. Two other numbers were wrong at the same time - `llm_call_rate` read 96%
because it counted every ingested row as a "narration", including gateway and ledger
rows that carry no free text at all.

**Decision 1 - pairwise precision and recall.** A *pair* is a claim that two rows
belong together. Precision is the share of claimed pairs that are true; recall the
share of true pairs that were claimed. This is the standard record-linkage measure and
the only one that means anything when settlement groups are many-to-one. Only
**cross-source** pairs count: two invoices sitting in the same settlement is not a
reconciliation claim, an invoice matched to a bank credit is.

The honest M6 baseline that falls out is **precision 1.000, recall 0.0599** over 3,808
true pairs. Stage 1 is *supposed* to be low-recall - it makes only the claims that
cannot be wrong, leaving a smaller and harder problem for the solver. A test now
asserts both halves, so a later milestone cannot quietly trade precision for coverage.

**Decision 2 - the denominator of a rate must be the population the claim is about.**
`llm_call_rate` is measured over bank statement rows, because a bank narration is the
only place a bank row's matching keys live; gateway and ledger rows carry structured
fields and would never be sent to a model. The reason is written into the metric's own
description so it can be argued with, and `llm_calls_absolute` reports the raw count
over every ingested row so neither framing hides behind the other. On the seed: **1
model call across 535 rows**.

**Decision 3 - the evaluator lives outside every matching package.** `evaluate_pipeline`
was originally inside `recon/pipeline.py`, which immediately broke the M5 test
asserting that `recon/` cannot reference the ground truth. Moving it to
`scripts/eval_pipeline.py` was the right response rather than relaxing the test: a
"the matcher cannot see the labels" guarantee only holds if the matcher genuinely
cannot import them.

**Decision 4 - no wall-clock in `metrics.json`.** Adding per-stage timings broke the
byte-identical guarantee within minutes of it being written. Timings stay on
`ReconResult` for the UI's stage ladder; throughput belongs to `make bench`, accuracy
to `make eval`.
