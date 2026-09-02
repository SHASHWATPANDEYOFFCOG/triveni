# Metrics history (append-only)

Every row here was produced by `make eval`, which writes `metrics.json`
deterministically. Rows are appended, never edited - including the rows where a
number got worse. Rule A.6: if a metric regresses after a change, it is reported.


> **Correction (M7).** The M6 row's recall was measured against a denominator of
> 3,808 "true pairs", which was wrong. It took the cross product of every payment and
> every invoice in a settlement group - claiming 196 pairs for a 14-payment
> settlement when there are 14 - because the pairing was inferred from group
> co-membership rather than recorded. Ground truth now stores the explicit 1:1
> payment-to-invoice links and the gateway settlement id, so the correct figure for
> the same dataset is 731. The M6 row is left in place because rows here are never
> edited; its recall figure is superseded by the M7 row, which measures the same
> deterministic stages against the corrected denominator.

| date | milestone | dataset | match rate | precision | recall | unexplained ₹ | LLM call rate | notes |
|---|---|---|---|---|---|---|---|---|
| _(first row lands at M6, the first milestone that touches the pipeline)_ | | | | | | | | |
| seed | M6 | 535 | 85.23% | 100.00% | 5.99% | ₹4,38,292.38 | 5.00% | digest db5e062c |
| seed | M7 | 535 | 90.84% | 100.00% | 33.24% | ₹33,553.07 | 5.00% | digest 2080b26d |
| seed | M8 | 535 | 94.95% | 100.00% | 34.75% | ₹18,994.55 | 5.00% | digest 2080b26d |
| seed | M9 | 535 | 95.70% | 91.69% | 81.53% | ₹0.00 | 5.00% | digest 2080b26d |
| seed | M10 | 535 | 95.51% | 97.73% | 82.63% | ₹2,021.31 | 5.00% | digest 2080b26d |
| seed | M11 | 536 | 95.34% | 98.06% | 82.90% | ₹2,677.10 | 4.76% | digest c5653fbf |
| seed | M12 | 536 | 95.34% | 98.06% | 82.90% | ₹2,677.10 | 4.76% | digest c5653fbf |
