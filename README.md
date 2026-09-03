<div align="center">

# त्रिवेणी · Triveni

**The AI Finance Controller that closes a merchant's books every day.**

Three ledgers — gateway, bank, internal — reconciled into one balanced set of books with a
*statistically guaranteed* error bound, every rupee of difference explained, the cash that is
actually landing forecast, and all of it proved by a tamper-evident, cryptographically
verifiable audit log.

_Razorpay AI Buildathon 2026 · Track 04 · AI Finance Controller_

</div>

---

> Every number in this README was produced by a script in this repo, from data in this repo,
> and is reproducible by you. The command that regenerates each one is printed beside it.
> Where a figure is an assumption rather than a measurement, it says so.

## The problem, in rupees

An Indian SMB on a payment gateway receives **one netted settlement per day, not one line per
sale**. Between the order, the payment, the refund, the MDR, the 18% GST on that MDR, the 0.1%
TDS under section 194-O, the chargeback hold and the T+2 timing offset, the amount that lands in
the bank almost never equals the amount in the books. Somebody reconciles that by hand, in a
spreadsheet, every single day.

```
  [assumption] 600 txns/day  ×  [assumption] 5% that break     =  30 exceptions/day
  30 exceptions              ×  [assumption] 4 minutes each    =  120 minutes/day
  2.0 hours                  ×  [assumption] ₹600/hour         =  ₹1,200/day
  × [assumption] 24 working days × 12                          =  ₹3,45,600/year
```

**₹14,080 to ₹83.20 L a year, per merchant, on reconciliation alone** — base case **₹3.46 L**.

The width of that band is the point. It comes from five stated assumptions and should be read as
one; the low case takes the optimistic end of every input at once and the high case the
pessimistic end. Not one input is sourced from a published study, and rather than dress a guess
as a finding, `size_the_problem.py` prints the chain so you can argue with it an input at a time.

```bash
python -m scripts.size_the_problem   # [verify] the chain above, and the measured half below
```

Razorpay names this problem itself: reconciliation is called out as a core merchant burden, and
the **Cashflow Forecaster Agent** and the reconciliation burden are both on the Agent Studio
roadmap. Triveni builds the part that is still done by hand — and proves the accuracy.

## 60-second quickstart

```bash
make setup     # creates .venv, installs pinned deps. No API keys. No Razorpay account.
make demo      # the whole narrative, offline, on committed seed data, in ~10 seconds
make run       # the dashboard at http://127.0.0.1:8000/app/
```

On Windows without GNU make, `make.cmd` is an identical entry point, or call
`python tasks.py <target>`. `make help` lists every target.

## What it measurably does

Measured on the committed 536-row seed. `make eval` regenerates all of it and produces a
**byte-identical `metrics.json`** across processes and across `PYTHONHASHSEED` values.

| | | |
|---|---:|---|
| rows reconciled in one run | **536** | across three sources in three formats |
| placed into an accepted match group | **95.34%** | 270 match groups |
| **posted with no human at all** | **72.82%** | at a *calibrated* confidence ≥ 0.9548 |
| precision of those unsupervised postings | **100.00%** | 450 claims |
| **error realised on held-out data** | **0.00%** | against a promised 1.00% bound |
| pairwise precision / recall | **98.06% / 82.90%** | over 731 true cross-source pairs |
| settlements balancing to ₹0 | **16 / 16** | rates fitted, R² 0.907 |
| rupees the waterfall could not explain | **₹2,677.10** | of ₹3,63,433 gross |
| left for a person | **87** | typed exceptions, each evidenced |
| rows that ever reach a model | **13 / 536 = 2.4%** | 39 calls — each sampled 3× |

Bootstrap 95% confidence intervals accompany every rate in `metrics.json`. The cost model's
six parameters are **stated illustrative assumptions**, labelled as such in the code, in the eval
output and in the run report.

```bash
make eval      # → metrics.json, byte-identical across runs
make bench     # throughput per stage, no warm-up discarded, no extrapolation
```

## Where we used an LLM / where we deliberately did not / why

**An LLM is for language and messy free text. Money, math, matching and thresholds are decided
by deterministic code, combinatorial optimisation and small calibrated models.** The third column
is the receipt.

| Layer | Who decides | Why | Proof |
|---|---|---|---|
| Parsing a bank narration into structured fields | **LLM**, temp 0, JSON schema | genuinely messy natural language | [`recon/normalize.py::parse_narration`](recon/normalize.py) — but see below |
| Deciding *which* records match | **CP-SAT + Hungarian + Fellegi–Sunter** | assignment is constrained optimisation, not a vibe | [`recon/assign.py`](recon/assign.py), [`recon/subsetsum.py`](recon/subsetsum.py) |
| Deciding *how much* a fee/GST/TDS delta is | **Closed-form arithmetic + robust NNLS** | it is algebra | [`recon/waterfall.py`](recon/waterfall.py), [`recon/fees.py`](recon/fees.py) |
| Deciding *whether we are confident enough to post* | **Split conformal calibration** | a guarantee beats a hunch | [`core/conformal.py`](core/conformal.py) |
| Classifying a residue row and writing its why-string | **LLM**, grounded, k-sample agreement | language | [`recon/escalate.py`](recon/escalate.py) |
| Answering "why is today's payout short?" | **SQL over verified numbers**, LLM only narrates | numbers come from the database, never the model | [`qa/compile.py`](qa/compile.py), [`qa/narrate.py`](qa/narrate.py) |
| Forecasting cash | **Model ladder + conformal intervals** | time series is not a chat problem | [`forecast/models.py`](forecast/models.py) |

**The number that makes this checkable: 13 of 536 rows ever reach a model — 2.4%.** Everything
else is resolved deterministically.

Two separate measurements, and it is worth keeping them apart because conflating them would
flatter us. **Narration parsing:** exactly **1 of 21** bank narrations needed a model, because 95%
of them are a *format* — `NEFT-UTIB940235305794-RAZORPAY SOFTWARE PVT LTD-SETTLEMENT` — and a
format is a regex, not a prompt. **Residue classification:** the 13 rows that survived every
deterministic stage go to the model to be *typed and explained*. Those 13 rows cost **39 calls**,
because each is sampled three times and the row abstains unless the *decisions* agree — so the
honest headline is 39 calls, not 13, and certainly not 1.

In both places the model **classifies and explains; it never picks a match, never computes an
amount, never sets a threshold**.

## The guarantee, and what it assumes

The auto-post threshold is not a number somebody tried once. It is fitted by **split conformal
calibration under conformal risk control**, taking the most permissive threshold whose empirical
error clears a finite-sample-corrected budget:

```
q̂ = inf { t : R̂(t) ≤ α − (B − α) / n }
```

| | before (hard-coded 0.90) | after (calibrated at α = 1%) |
|---|---:|---:|
| threshold | 0.90 *chosen* | **0.9548 fitted** |
| auto-post coverage | 41.1% | **72.8%** |
| auto-post precision | 100% | **100%** |
| realised error, held-out | *not measured* | **0.00%** against a 1.00% bound |

**On a single split, α = 2% breaches** — 2.64% realised against 2.00% promised. That is reported,
not hidden, and it is not a bug: conformal risk control bounds error *in expectation over the
draw of the calibration set*, so an individual split can exceed it. `validate_guarantee` re-splits
40 times, every bound holds on the mean, and the **per-split breach rate is reported too** —
because "the mean holds and 2% of runs breach" and "the mean holds and 33% breach" are different
products.

**The assumption, stated plainly.** Conformal prediction requires *exchangeability*, and
reconciliation data violates it routinely: a gateway changing its settlement schedule shifts the
timing distribution; a new payment method has a fee structure the model has never seen; month-end
is not exchangeable with mid-month; and calibration labels exist precisely because a human looked
at those rows, which is not a random sample. `Calibration.assumption_report()` says all of that
in the output rather than a footnote.

```bash
python -m scripts.calibrate    # the coverage table and the 40-split validation
```

## Architecture

One spine, three loops. Full detail in [`docs/architecture.md`](docs/architecture.md).

```
                       ┌───────────────────────── THE SPINE ─────────────────────────┐
  gateway ─┐           │  ingest → canonicalise → ledger core → Merkle audit log     │
  bank ────┼──────────▶│  policy engine · materiality gate · conformal calibrator    │
  ledger ──┘           │  eval harness · ₹ cost model · deterministic clock + seed   │
                       └───┬──────────────────┬──────────────────────┬───────────────┘
                           │                  │                      │
                     LOOP 1: CLOSE      LOOP 2: EXPLAIN        LOOP 3: FORESEE
                  3-way reconciliation   settlement            cash-flow forecast
                  + typed exceptions     decomposition +       with conformal
                  + human triage         grounded Q&A          intervals + alerts
```

Seven stages, cheapest and most certain first. **A later stage may only add** — one that wants to
overwrite an earlier match raises a conflict and routes to a human, which is what makes each
stage's contribution real rather than a re-run.

| stage | what it contributed on the seed |
|---|---|
| 0 · canonicalise | 536 rows normalised; 20/21 narrations parsed with no model; 1 injection denied |
| 1 · deterministic keys | +253 — UTR matched 15 settlements, order-id matched 238 invoices |
| 2 · blocking | 5,688 candidate pairs, **reduction ratio 0.9199** against 71,043 comparisons |
| 3 · Fellegi–Sunter | scored 8 open pairs, linked 1 at ≥ +6.0 bits |
| 4 · global assignment | +16 — 182 payments attributed via CP-SAT, **proven optimal** |
| 5 · settlement decomposition | **16/16 waterfalls balance to ₹0**, rates fitted R² 0.907 |
| 6 · residue | 13 rows to a model; 0 denied; the only stage that calls one |

## The audit log is a Merkle transparency log, not a hash chain

This is the RFC 6962 construction that secures the web PKI, applied to a financial decision log.
A hash chain cannot answer either question an auditor actually asks: proving record 4,000 is
present needs a full replay, and it **cannot prove append-only at all** — an operator who rewrites
history and re-chains produces a log where every individual link still verifies.

- **inclusion** in 17 hashes for a 100,000-record log (measured, not asserted)
- **consistency** — the property that makes "append-only" a proof rather than a promise
- **Ed25519-signed tree heads**, so an attacker who rewrites *and* recomputes the root still fails

Tamper with record 4 of 12 and the verifier reports **index 4**, *and* that heads 1–4 still verify
while 5–11 do not — locating the tampering in time as well as position.

```bash
make verify                                # re-derive the root, locate any tampering
python -m core.audit.verify --tamper 4     # break it on purpose and watch the proof fail
```

## Failure recovery — each one a single command

| what breaks | what happens | try it |
|---|---|---|
| prompt injection in a bank narration | denied at **ingest**, logged with matched patterns | `make redteam` |
| a corrupt CSV row | kept as a typed `corrupt_row` exception with its raw payload | `make demo` |
| an ambiguous many-to-one | abstains with evidence rather than guessing | `make demo` |
| a tampered audit row | proof fails at the exact index | `python -m core.audit.verify --tamper 4` |
| an unanswerable question | abstains and shows the table it would have used | `python -m scripts.qa_adversarial` |
| the model switched off entirely | matching is unchanged at 270 groups | `TRIVENI_LLM_MODE=off make demo` |

**`make redteam` — 13 cases, all passing.** Injection denied with evidence, delimiter escape
defeated by a content-derived nonce, schema violation abstains, budget exhaustion abstains, forged
audit receipt cannot post, money movement refused with no config field that could enable it.

**`python -m scripts.qa_adversarial` — 12 adversarial questions all abstain, 4 answerable ones all
answered with every figure traced to a row.** The answerable half is not decoration: a system that
abstains on everything is not grounded, it is mute.

## Razorpay rails

Triveni runs offline by default (`TRIVENI_RAILS=offline`, recorded fixtures). Set
`TRIVENI_RAILS=mcp` to read from the official `razorpay/razorpay-mcp-server`. Both rails satisfy
the same protocol and land in the same reader, so a contract test asserts the live one cannot
drift into a different schema.

**Triveni may call 10 read tools and 0 write tools.** A finance controller reads and proposes; it
does not move money. Enforced three ways rather than promised once: an allowlist of literal names
(not a pattern — `fetch_*` would admit a future `fetch_and_capture`), an **import-time assertion**
that the 11 named write tools never appear in it, and `READ_ONLY=true` sent to the server.

### Triveni's own MCP server — adopt it in one call

```python
from api.mcp_server import TriveniMCP

triveni = TriveniMCP()                               # offline, no credentials
close = triveni.call("close_books", {"date": "2026-03-31"})

print(close["reason"])                               # every response explains itself
print(close["conformal_threshold"], close["realised_error_holdout"])

for exception in triveni.call("list_exceptions", {"severity": "high"})["exceptions"]:
    print(exception["type"], exception["reason"])

print(triveni.call("verify_audit", {})["reason"])    # the books prove themselves
```

Seven tools, each returning a mandatory `reason` field. Exactly one mutates state:
`resolve_exception` records a *human's* decision, refuses without both an approver and a reason,
and returns the Merkle inclusion proof so the caller can verify the record **without trusting the
response that carried it**.

## The dashboard

Eight screens, **197 KB, zero dependencies, zero build step**, served by the same process as the
API. That is a deliberate trade ([ADR 0018](docs/adr/0018-a-zero-build-dashboard.md)): `npm
install` needs hundreds of megabytes over the network, which breaks the hard requirement that
everything runs cold on a clean clone.

Both themes as full token sets, money always tabular-nums with Indian digit grouping, every
animation a no-op under `prefers-reduced-motion`, keyboard-complete, responsive to 390px.

The three money-shots: **the α slider** moving coverage, error bound and ₹ cost together as a
visible step function; **the settlement waterfall** landing on *balanced to ₹0*; and **the tamper
simulation** breaking the Merkle proof at the exact index.

## Limitations

Kept honestly in [`docs/limitations.md`](docs/limitations.md). The headlines:

- **The data is synthetic.** No real merchant data. Generated from a fixed seed with ground-truth
  labels; the generator refuses to emit a dataset whose own labels do not balance.
- **The LLM cassettes are synthetic fixtures, not recordings of a live model.** This build has no
  API key, and writing invented outputs into a file labelled "recorded" would be dishonest. Every
  entry is stamped `"source": "synthetic-fixture"` and the gateway says so in its report.
- **Recall is 82.90%, not 99%.** 68 payments the solver could not attribute became
  `missing_in_bank` exceptions rather than being forced into a group.
- **`docker compose up` is written but unverified** — no Docker on the build machine.
- Forecast coverage realised **88.1% against a 90% nominal** — inside one standard error at n=84,
  and reported that way rather than as either a pass or a failure.

## Reproduction

```bash
make test      # 765 tests: unit · property · golden · e2e
make eval      # → metrics.json, byte-identical across runs
make bench     # throughput per pipeline stage
make verify    # re-derive the Merkle root, locate any tampering
make redteam   # 13 adversarial cases
make demo      # the whole narrative, offline, ~10 seconds
python -m scripts.calibrate        # the conformal coverage table
python -m scripts.qa_adversarial   # 12 must abstain, 4 must answer
python -m scripts.size_the_problem # the rupee chain above
```

`mypy --strict` clean on `core/` and `recon/`; `ruff` clean; a custom AST lint proves no float can
enter a money path.
