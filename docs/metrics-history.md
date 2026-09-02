# Metrics history (append-only)

Every row here was produced by `make eval`, which writes `metrics.json`
deterministically. Rows are appended, never edited - including the rows where a
number got worse. Rule A.6: if a metric regresses after a change, it is reported.

| date | milestone | dataset | match rate | precision | recall | unexplained ₹ | LLM call rate | notes |
|---|---|---|---|---|---|---|---|---|
| _(first row lands at M6, the first milestone that touches the pipeline)_ | | | | | | | | |
