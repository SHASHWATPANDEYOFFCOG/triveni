# ADR 0009 - Blocking is a scheme, measured per strategy

**Status:** accepted - 2026-09-02

**Context.** On the 5,000-row dataset a brute-force matcher would compare 7.36 million
cross-source pairs, of which roughly 25 thousand are real. Blocking decides which
pairs are worth scoring, and it is the one stage whose mistakes are unrecoverable:
anything it discards, no later stage can find. Pair completeness is therefore a hard
ceiling on the recall of the entire pipeline.

**Decision.** Five strategies, unioned, each **restricted to the relation it serves**
and each measured separately for pair completeness and reduction ratio.

| strategy | relation | why it exists |
|---|---|---|
| `reference` | all | a shared UTR / RRN / order id is the strongest signal there is |
| `settlement_window` | gateway or ledger to **bank** | the netting relation: a credit is the *net* of many payments, so the date is the only thing they share |
| `counterparty` | all | fallback when the reference is absent |
| `amount` | gateway to ledger | the 1:1 workhorse - an invoice and its payment agree to the paise |
| `dense_ann` | all | truncation, casing and unseen formats, via hashed character 3-grams |

**Measured, on the committed seed and on 5,481 rows:**

| dataset | possible pairs | candidates | reduction ratio | pair completeness |
|---|---:|---:|---:|---:|
| seed (520 rows) | 73,130 | 5,672 | 0.9224 | **1.0000** |
| full (5,481 rows) | 7,364,041 | 63,300 | 0.9914 | **1.0000** |

**What measuring per strategy actually bought.** Three separate improvements that a
single "blocking works" number would have hidden:

1. The **log-scale amount buckets** added 10,236 candidate pairs on the seed for *zero*
   additional pair completeness. Deleted.
2. **Token-level counterparty keys** did not scale: `cp:TEXTILES` collected 655,122
   pairs on the full dataset - 96% of the entire union - for coverage the reference
   blocker already had. Keyed on the whole normalised name instead, which dropped that
   strategy from 655,122 pairs to 17,011 with no loss of PC.
3. The **settlement-window blocker** was projecting T+2 from a gateway settlement row,
   whose date already *is* the settlement date. It was looking for the credit two days
   after it landed, and cost real completeness on split settlements.

**Why the embedding is local.** No `torch`, no `sentence-transformers`, no download:
a hashed character 3-gram projection, ~40 lines, deterministic via blake2b rather than
Python's salted `hash()`. This is a stated trade. A transformer encoder would very
likely win on paraphrase, but bank narrations are not paraphrase - they are truncation
and formatting noise, which character n-grams handle well - and a cold offline
`make demo` must not download gigabytes. The neural slot stays feature-flagged and off;
`docs/limitations.md` says so plainly.
