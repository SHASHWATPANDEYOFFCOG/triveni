# Architecture

One spine and three loops. The spine holds everything that must be true regardless of
which loop is running - money, time, identity, the audit log, the policy engine, the
calibrator, the evaluation harness. The loops are the product.

The rule that shapes the whole tree: **`core/` contains no feature logic.** Loops may
import `core/`; they may never import each other. A module that needs to reach across
loops is a design smell and gets refactored into the spine instead.

```
core/                    THE SPINE - no feature logic lives here
  money.py               integer paise, never a float; Rate carries its rounding mode
  clock.py               injectable Clock; IST as a fixed offset; the bank calendar
  ids.py                 canonical JSON (one byte string per value) + content-addressed ids
  errors.py              typed error hierarchy; no bare except anywhere
  audit/merkle.py        RFC 6962 tree, inclusion and consistency proofs
  audit/log.py           append-only SQLite, Ed25519-signed tree heads
  audit/verify.py        `make verify` - the independent auditor
  policy.py              pure, total: materiality, caps, gates → Decision
  guard.py               the ONLY door to the books; receipt-gated
  conformal.py           split conformal calibration + risk control
  costmodel.py           the ₹ cost of being wrong, asymmetric
  eval.py                metric registry, bootstrap CIs, byte-identical metrics.json
  llm.py                 the ONE gateway; schema, budget, cassettes, injection defence

ingest/                  three sources → one schema
  canonical.py           CanonicalTxn - the single internal shape
  adapters/fixtures.py   Razorpay-shaped JSON, offline
  adapters/csv_bank.py   bank statement + ledger CSV; hostile formats absorbed here
  adapters/razorpay_mcp.py  the live rail, behind a read-only allowlist

recon/                   LOOP 1 - CLOSE
  normalize.py           Indian name/reference normaliser + narration parsing
  blocking.py            deterministic ∪ dense blocking; PC/RR reported
  fellegi_sunter.py      EM-fitted m/u, per-field weights, explainable score
  assign.py              global min-cost assignment (Hungarian)
  subsetsum.py           exact many-to-one selection (meet-in-the-middle + CP-SAT)
  fees.py                MDR/GST/TDS/FX rate fitting (robust NNLS)
  waterfall.py           gross → net, itemised, balanced to the paise
  escalate.py            the residue explainer; abstains into a typed exception
  exceptions.py          the closed 16-type taxonomy + evidence bundles
  pipeline.py            the staged orchestrator; emits a StageReport per stage

forecast/                LOOP 3 - FORESEE
  features.py            calendar (bank holidays, salary days, GST dates) + lags
  models.py              the ladder: naive → drift → Theta → ETS → boosted quantile
  backtest.py            rolling origin, conformalised intervals, measured coverage
  alerts.py              the liquidity rule, on the LOWER bound
  service.py             the loop, end to end

qa/                      LOOP 2 - EXPLAIN (the question half)
  semantic_view.sql      the ONLY surface the Q&A may query, documented per column
  warehouse.py           loads a run into DuckDB behind three views
  compile.py             NL → one of ten hand-written templates; never generated SQL
  narrate.py             the numeral-grounding verifier; abstains on a mismatch

api/
  main.py                FastAPI: 13 routes, SSE, and the dashboard at /app/
  mcp_server.py          Triveni's own MCP server - seven tools, one of them mutating

web/                     the dashboard: 8 screens, zero build, zero dependencies
```

## The seven stages, and why they are in that order

Cheapest and most certain first, so every later stage inherits a smaller and harder
problem. Each stage may only **add** matches; one that wants to overwrite an earlier
match raises a conflict and routes to a human. That monotonicity is what makes the
stage ladder in the UI an honest attribution rather than a re-run.

| # | stage | what it is | why here |
|---|---|---|---|
| 0 | canonicalise | normalise money, dates, references, names; parse narrations | everything downstream assumes one shape |
| 1 | deterministic keys | exact UTR/RRN, and (order-id, amount, window) | claims that cannot be wrong. Free. |
| 2 | blocking | deterministic ∪ dense candidate generation | makes the quadratic problem linear-ish |
| 3 | Fellegi–Sunter | EM-fitted per-field weights, log-odds score | an explainable score, not a black box |
| 4 | global assignment | min-cost matching + exact subset selection | the whole day at once, so nothing double-books |
| 5 | decomposition | fit the rates, build the waterfall | turns a difference into named components |
| 6 | residue | classify what is left, or abstain | the only stage that calls a model |

**Stage 4 is the one that matters and the one that costs.** Pairwise scoring produces a
scored bipartite graph; the answer is the maximum-weight matching on that graph, not the
greedy argmax. Taking the best pair for each row independently double-books rows and
produces a set of locally plausible, globally impossible matches. Many-to-one netting is
solved as an exact subset-sum under tolerance, disambiguated by CP-SAT with the
constraint that each invoice is used at most once *across the whole day*.

## Six invariants, each enforced in code

1. **Money is integer paise.** A float in a money path is a build failure —
   `scripts/money_lint.py` walks the AST of every money-path module and rejects float
   literals, `float()` calls and non-Decimal division, with a reason-mandatory escape
   hatch. Hypothesis generates arbitrary floats against every constructor and operator.
2. **Conservation.** `Σ matched_gateway = Σ matched_bank ± Σ explained_deltas`, to the
   paise. The generator refuses to emit a dataset whose own labels violate it.
3. **No double-spend.** Every source row appears in at most one accepted match group;
   asserted after every stage.
4. **Monotone stages.** A later stage may only add. Conflicts are refused and counted.
5. **Audit before act.** `core/guard.py::_post_to_books` requires an `AppendReceipt`
   carrying a Merkle inclusion proof under an Ed25519-signed head, checks the audited
   record names *this* subject, and checks its verdict permits posting. A caller who
   skipped the log cannot manufacture one without forging a signature.
6. **Abstain is a value.** `LLMOutcome.abstained` and `ReconException` are ordinary
   return values, never exceptions and never a silent default match.

## Determinism

`make eval` twice produces a byte-identical `metrics.json`, across processes and across
`PYTHONHASHSEED` values. What that required:

- **no wall-clock in business logic** — everything takes a `Clock`, and an AST test
  asserts nothing outside `core/clock.py` reads `datetime.now()`;
- **no timestamp in the report** — a file that records when it ran cannot be compared
  against itself, so provenance is the seed plus a digest of the source that produced it;
- **canonical JSON** with one byte string per value, floats refused outright;
- **explicitly constructed PCG64 streams**, so the bootstrap depends only on its seed and
  not on how many draws happened earlier in the process;
- **every real quantised through `Decimal`** before serialisation;
- **wall-clock excluded from `metrics.json`** and left to `make bench`, because a timing
  that varies by a millisecond breaks byte-identity.

## Where the LLM sits, structurally

One gateway, `core/llm.py`, and nothing else calls a model. It enforces: strict JSON
schema validation with abstention rather than coercion; untrusted source text wrapped in
a **content-derived nonce delimiter** so a narration containing `</source>` cannot close
the block early; a rupee budget that halts; offline cassette replay that raises loudly on
a miss rather than falling back to a live call; and **k-sample agreement measured on the
decision rather than the token string** — a model that says "timing difference" and
"settlement timing" has agreed about what to do.

Injection is scanned **at ingest**, not at the model boundary. Scanning only where text
reaches a model meant the seed's attacking narration was resolved by Stage 4 and the
denial never appeared — the defence worked and was invisible.

## Data flow, one close

```
  fixtures/CSV ──▶ canonicalise ──▶ blocking ──▶ scoring ──▶ solver ──▶ waterfall ──▶ residue
                        │                                       │            │           │
                        ▼                                       ▼            ▼           ▼
                   corrupt rows                            exceptions   unexplained   abstentions
                        │                                       │            │           │
                        └───────────────────┬───────────────────┴────────────┴───────────┘
                                            ▼
                                    typed exception queue
                                            │
                        ┌───────────────────┼───────────────────┐
                        ▼                   ▼                   ▼
                 conformal gate        human triage        audit log (Merkle)
                        │                   │                   │
                        └──── auto-post ────┴──── approved ─────┘
                                            │
                                            ▼
                                    guarded posting
```

Every arrow into "guarded posting" passes through `core/policy.py` first and
`core/guard.py` second. There is no other path to the books.

## Deliberate omissions

- **No `torch` / `sentence-transformers`.** A cold `make demo` must not download
  gigabytes. Dense blocking uses a local deterministic hashed character n-gram
  embedding; the neural slot is feature-flagged off.
- **No time-series foundation model.** Same reason, feature-flagged off. If the boosted
  baseline wins — and it does, at MASE 0.546 — that is published as-is.
- **No component library on the front end.** See
  [ADR 0018](adr/0018-a-zero-build-dashboard.md).
- **No write capability anywhere.** Not disabled — absent. There is no code path from any
  alert, tool or endpoint to a transfer, which is why there is nothing to talk into one.
