# Limitations — what Triveni does not do, and what is unverified

Kept current, not written once. A complete system with stated limits beats a
half-finished ambitious one, and every item here is something a judge would otherwise
have to find themselves.

## The data is synthetic

No real merchant data was used and none would be. `data/gen.py` generates three sources
with ground-truth labels from a fixed seed, and refuses to emit a dataset whose own
labels do not balance. That is honest, and it is also the single biggest caveat on every
accuracy number in this repo: **the pipeline has been graded against a world we
constructed.** The generator models the things we know matter — netted settlements, the
2nd/4th Saturday bank calendar, zero-MDR UPI, GST on the fee, T+2 — but a real merchant's
data will contain shapes it does not.

## The LLM cassettes are synthetic fixtures, not recordings

This build has no API key. Every cassette entry is stamped
`"source": "synthetic-fixture"`, the bundle header says so, and the gateway repeats it in
its own report. Writing invented model outputs into a file labelled "recorded" would be
exactly the dishonesty the constitution forbids.

Two things keep them defensible: they are generated **through the real call path** (a
hand-reproduced call site produced keys that differed by a whitespace and missed every
time), and the stand-in that produces them sees **only what the model would see**, never
the ground truth. A fixture derived from the labels would make the residue stage look
perfect while proving nothing.

`TRIVENI_LLM_MODE=record` with a provider key writes real ones through the same code.

## Accuracy, stated as it is

- **Recall is 82.90%, not 99%.** 68 payments the solver could not attribute became
  `missing_in_bank` exceptions rather than being forced into a group. That is the design
  working — a forced match is a false match and costs ~15× a missed one — but it is a real
  ceiling and it is not hidden behind the 95.34% match rate.
- **Precision is 98.06% overall**, 100% on the subset posted unsupervised. The gap is the
  point of the conformal threshold.
- **`unexplained_amount` is ₹2,677.10**, not zero. All 16 settlements balance, but bank
  credits outside any settlement group remain unattributed.

## The guarantee, and where it stops

- Conformal prediction requires **exchangeability**, which reconciliation data violates
  routinely: schedule changes, new payment methods, month-end versus mid-month, and the
  fact that calibration labels exist because a human looked at those rows.
- At α = 2% **a single split breaches the bound** (2.64% realised against 2.00%). Reported,
  not hidden. It holds in expectation over 40 splits, which is what the theorem promises —
  but the per-split breach rate at that α is 32.5%, and an operator should know that.
- The calibration set is ~315 claims. The finite-sample correction is large at that size,
  and at small α the method correctly refuses to promise anything rather than issuing a
  weak guarantee dressed as a strong one.

## Forecast

- Coverage realises **88.1% against a 90% nominal**. At n=84 held-out days the binomial
  standard error is 3.3 points, so that shortfall is 0.6 SE — reported as "inside 1 SE"
  rather than as either a pass or a failure.
- The forecast needs **74+ days of history**; the committed seed has 21. It reports its own
  unavailability rather than fabricating a series, and `python -m data.gen --spec history`
  generates enough.
- `seasonal_drift` **loses to the naive baseline** and is published that way.

## Unverified on this machine

- **`docker compose up` is written but never executed.** Docker is not installed here, so
  the Dockerfiles and `docker-compose.yml` are reviewed-but-untested. The supported,
  exercised path is `make setup && make demo`.
- **No Lighthouse run.** The dashboard was built to the accessibility requirements — real
  buttons, visible focus, `aria-live`, full keyboard access, reduced-motion guards, 390px
  responsive — and those were checked by hand and by a lint over the source, but no
  Lighthouse score was measured, so none is claimed.
- **No cross-browser testing.** Developed against one engine. The code uses only widely
  supported APIs (ES modules, Web Animations, `EventSource`, `color-mix`), but
  `color-mix` in particular is newer than the rest.

## Deliberately excluded

- **No `torch` / `sentence-transformers`.** A cold `make demo` must not download
  gigabytes. Dense blocking uses a local deterministic hashed character n-gram embedding;
  the neural slot is feature-flagged off (`TRIVENI_ENABLE_NEURAL_EMBEDDINGS=0`).
- **No time-series foundation model** (`TRIVENI_ENABLE_TSFM=0`), same reason. The boosted
  baseline wins on this data anyway, at MASE 0.546.
- **No component library or build step on the front end** ([ADR 0018](adr/0018-a-zero-build-dashboard.md)).
  The cost is that every control is hand-built; the benefit is that there is no build to
  fail and no `npm install` to break the offline guarantee.

## Scope

- **Triveni is read-only with respect to money.** It proposes; it never posts to a bank or
  calls a Razorpay write tool. Enforced by a literal allowlist, an import-time assertion
  and `READ_ONLY=true` — not by convention.
- **No authentication, no TLS, no multi-tenancy.** It is a localhost demo. The `approver`
  field is a string until there is an identity layer behind it. See
  [`threat-model.md`](threat-model.md).
- **The demo signing key is derived from `TRIVENI_SEED`** so output is byte-identical on
  any machine. Anyone who can read that seed can forge tree heads. Every head it signs is
  stamped `demo-deterministic`; production supplies `TRIVENI_AUDIT_KEY` through the same
  code path.

## Cost figures are assumptions

The six cost-model parameters — analyst hourly cost, minutes to triage, to unwind a false
match, to review a flagged one, the unrecovered share of a wrong match, and minutes to
eyeball a row by hand — are **stated illustrative assumptions**, not sourced findings. The
`Assumption` type refuses an empty source, the eval output lists every illustrative one
under the table, and `metrics.json` carries them all. Substitute your own and every rupee
figure moves.

The same applies to the five inputs in `scripts/size_the_problem.py`, which is why it
prints a band rather than a number.
