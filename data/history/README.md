# `data/history/` — the committed forecast input

The cash forecast is built from **reconciled bank credits**, not from gateway captures.
Captures tell you what was sold; only the bank side tells you what actually landed, and
payroll is paid out of the second one. So the only source file the forecast reads is
`bank_statement.csv`, and that is the only one committed here.

`forecast.json` is the cached backtest — six rolling-origin folds over five models takes
about ninety seconds, which is most of `make demo`'s whole budget for a section that is
not the point of the demo. It carries the digest of the series it was computed from, so
a stale cache is detected rather than silently believed.

Regenerate both:

```bash
python -m data.gen --spec history     # rewrites bank_statement.csv (and the rest, untracked)
python -m scripts.forecast_report     # recomputes forecast.json
```

The generator also emits gateway, ledger and ground-truth files for this span. They are
**not committed** because nothing reads them — the forecast needs the bank side only, and
2 MB of synthetic ledger rows that no feature opens is bloat, not provenance.
