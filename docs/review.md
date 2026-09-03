# Adversarial self-review

Scored against the four judging criteria, harshly, with file:line evidence. Written
after an adversarial review pass whose findings are listed at the bottom — including
the ones that were real, which is the part worth reading.

The rule for this document: **a score is only worth writing down if the thing that
would lower it is named.** Every section below states what is missing before the next
point, not just what is present.

---

## 1 · Problem taste — **4 / 5**

**What is there.** The README opens with an arithmetic chain in rupees where every
input is labelled `[assumption]`, produced by `scripts/size_the_problem.py` so the
document and the script cannot drift. It reports a band — **₹14,080 to ₹83.20 L a year,
base ₹3.46 L** — rather than a hero number, because five guessed inputs cannot support
one. The second half of that script is the measured half, read from `metrics.json`.

The problem chosen is the part still done by hand: not "show me my payments" but "one
netted settlement arrived and I cannot tell you what the difference is made of."
`data/gen.py:434` generates data with that shape — one credit per settlement day — which
is what makes `recon/subsetsum.py` necessary rather than decorative.

**Why not 5.** The five inputs are *my* assumptions. I could not find a checkable public
source for exceptions-per-thousand-transactions or minutes-per-exception at Indian SMBs,
and I would rather say so than cite something I have not read. A merchant's own numbers
would make this section far stronger, and I do not have them. The Razorpay Agent Studio
tie-in is asserted from public positioning rather than from a document I can link.

---

## 2 · Build quality — **4 / 5**

**Evidence.**
- Clean clone → `make setup && make demo` → the full nine-beat narrative in **8.4s**,
  no keys, no network. Verified by deleting `data/generated/`, `metrics.json` and
  `.triveni/audit.db` and re-running.
- **837 tests**, `mypy --strict` clean on `core/` and `recon/`, `ruff` clean.
- `make eval` twice is **byte-identical**, across processes and under
  `PYTHONHASHSEED=random` (`tests/test_eval.py:42`, `:57`).
- Six invariants enforced in code, not convention: the float lint
  (`scripts/money_lint.py:104`) with a reason-mandatory escape hatch and a test proving
  it is not vacuous (`tests/test_money.py:105`); receipt-gated posting
  (`core/guard.py:58`); no-double-spend asserted after every stage
  (`recon/pipeline.py:134`).
- Typed errors throughout (`core/errors.py`), no bare `except`.

**Why not 5.** Two things, both real.

The dashboard shipped with **zero tests** and an undefined function on its failure path
— `paintOffline` at `web/js/screens/forecast.js:102`, which threw a `ReferenceError`
precisely when the API was unreachable. That is a straightforward quality failure and it
survived a whole milestone because `make test` never opened `web/`.
`tests/test_dashboard.py` now exists and its undefined-identifier check was verified by
reintroducing the bug and watching it fail.

And `docker compose up` is written but never executed — no Docker on this machine. It is
reviewed-but-untested and `docs/limitations.md` says so rather than implying otherwise.

---

## 3 · AI judgment — **5 / 5**

This is the criterion the project is strongest on, and the evidence is a number rather
than a claim: **13 of 536 rows ever reach a model — 2.4%**, at 39 calls
(`metrics.json`: `rows_reaching_model`, `residue_llm_calls`).

- **Matching is optimisation.** `recon/assign.py` (Hungarian, with a dummy no-match
  column priced at the conformal threshold so declining is a modelled option) and
  `recon/subsetsum.py` (meet-in-the-middle + CP-SAT, proven optimal). A test asserts the
  model output cannot influence a match.
- **Fees are algebra.** `recon/waterfall.py` with rates *fitted* by robust NNLS
  (`recon/fees.py`), R² 0.907 — not hard-coded. GST on the fee, never on the sale.
- **The threshold is calibrated.** `core/conformal.py:145`, with the finite-sample
  correction that makes it a bound, and `assumption_report()` naming exchangeability as
  the thing reconciliation data breaks.
- **Language is where the model lives.** `recon/escalate.py` classifies into a closed
  16-value enum and writes one sentence. `qa/compile.py` never generates SQL — it selects
  among ten hand-written templates with validated parameters.
- **Grounding is enforced, not requested.** `qa/narrate.py:113` checks every numeral
  against the rows SQL returned and discards the whole answer otherwise.
  **12/12 adversarial questions abstain, 4/4 answerable ones answer with every figure
  traced** (`scripts/qa_adversarial.py`).

**The honesty correction that earned this score rather than undermined it.** The README
originally said *"1 model call across 536 rows."* That was wrong in the flattering
direction: it counted only narration parsing while the residue stage makes 39 calls. Two
different things were being measured and the smaller one had the general-sounding name.
Both are now separate named metrics produced by `make eval`, and the video script gained
a "do not say" entry for it. A cherry-picked denominator on the criterion that is
supposed to be about judgment would have been the worst possible place to have one.

---

## 4 · Failure recovery — **5 / 5**

Every one is a single command, and each fails in a *specific* way rather than merely
not-crashing.

| what breaks | what happens | command |
|---|---|---|
| prompt injection in a narration | denied **at ingest**, logged with matched patterns | `make redteam` |
| a corrupt CSV row | typed `corrupt_row` exception with its raw payload; batch continues | `make demo` |
| an ambiguous many-to-one | abstains with evidence and a stated reason | `make demo` |
| a tampered audit row | proof fails at the **exact index**, and dates the tampering | `python -m core.audit.verify --tamper 4` |
| an unanswerable question | abstains and shows the table it would have used | `python -m scripts.qa_adversarial` |
| the model switched off | matching unchanged at 270 groups | `TRIVENI_LLM_MODE=off make demo` |
| a forged audit receipt | `UnauditedAction`; the side effect never runs | `tests/test_policy.py` |
| no history for the forecast | reports its own unavailability, fabricates nothing | `forecast/service.py:126` |

**The one worth singling out.** Injection is scanned at *ingest*, not at the model
boundary. The first implementation scanned at the boundary, which meant the seed's
attacking narration was resolved by Stage 4 and the denial never appeared — the defence
worked and was invisible. A defence you cannot demonstrate is one nobody will believe.

---

## 5 · Craft / UI — **4 / 5**

Eight screens, **197 KB, zero dependencies, zero build step**, served by the API process
([ADR 0018](adr/0018-a-zero-build-dashboard.md)). Both themes as complete token sets with
`[data-theme]` beating `prefers-color-scheme` in both directions; money always
tabular-nums with Indian grouping; every animation a no-op under reduced motion, guarded
centrally in `tokens.css` so it cannot be forgotten per-component; keyboard-complete with
`aria-live` on async regions; responsive to 390px.

**Why not 5.** No Lighthouse run, so no score is claimed. No cross-browser testing. And
the screens were authored partly in parallel, which showed: the review found an
inconsistent `balanced` default that could stamp a green "balanced to ₹0" seal over books
that were short (`web/js/screens/waterfall.js:331`, now `=== true`), and a band whose
lower edge was drawn time-reversed. Both are fixed, but a UI that needed an adversarial
pass to find them is not a 5.

---

## The five non-negotiables, with evidence

| # | rule | where it is enforced |
|---|---|---|
| 1 | **EXPLAINABLE** | `core/audit/log.py:86` and `recon/exceptions.py:194` — `reason` is `min_length=1`, so a record without one cannot be constructed |
| 2 | **BOUNDED** | `core/policy.py:115,118` hard caps; `:202` `RULE_NO_MONEY_MOVEMENT` with a test asserting no config field could enable it |
| 3 | **GATED** | `core/policy.py:112` materiality, `:125` the conformal threshold; gates route to a human, bounds refuse outright |
| 4 | **AUDITED** | `core/guard.py:58` — posting requires a receipt carrying a Merkle proof under a signed head; four `UnauditedAction` paths |
| 5 | **MEASURED** | `core/costmodel.py:277` prices the outcome mix; `:174` `break_even_false_matches` is the number that governs the threshold |

## Disqualifier check

- **Fabricated numbers** — a script verifies all ten README headline figures against
  `metrics.json`. The one misleading claim found (the model-call count) is corrected and
  both measurements are now separate metrics. Cost parameters are `Assumption` objects
  that refuse an empty source.
- **Secrets in history** — `scripts/secretscan.py`, 9 patterns, wired into `make`, the
  test suite and a pre-commit hook. Clean across 157 tracked files.
- **A demo that works on one hand-picked case** — 536 rows on the seed, 5,481 on the full
  set, 16/16 settlements balancing, all 16 exception types present in both.
- **Unbounded autonomous money movement** — no capability exists. `ActionClass.MOVE_MONEY`
  is refused unconditionally; the Razorpay allowlist is 10 literal read tools with an
  import-time assertion that the 11 write tools never appear in it.
- **Offence-capable** — nothing here attacks anything. `scripts/redteam.py` attacks
  Triveni.

---

## What the adversarial review actually found

Five findings, all verified against the code before being accepted, all fixed.

1. **`paintOffline` called and never defined** (`forecast.js:102`) — a `ReferenceError`
   on exactly the failure path the surrounding comment describes. Root cause: no tests
   for `web/`. Fixed, and `tests/test_dashboard.py` now catches this class.
2. **The confidence band's lower edge drawn time-reversed** — `.reverse().reverse()` is
   the identity, so row 0's lower bound was plotted in the last column. Since the
   cumulative lower bound rises monotonically, the "band" was a crossed wedge on the one
   screen whose whole argument is "the alert measures from the lower bound."
3. **A hard-coded cost 5.26× the real one** (`alpha.js`) — 500,000 paise against the
   95,000 that `core/costmodel.py` computes, so the slider disagreed with
   `scripts/calibrate.py` for the same operating point. Fixed by *serving* the cost model
   at `/costmodel` so there is only one copy.
4. **Neither α the README argues about was reachable on the slider** — the geometric grid
   contained no 0.01 and no 0.02, so a judge could not land on the headline operating
   point or the documented breach. Anchored.
5. **A debounce leak desynchronising the α tiles** — returning early on a cache hit left
   a pending timer armed, which then repainted with a stale α. On the screen whose entire
   promise is "these numbers move together."

Plus one found independently while verifying the cold-start claim: **`make demo` beat 9
degraded on a clean clone**, because the forecast read a gitignored directory.
`data/history/` (32 KB) is now committed.

And one flaky test: `test_canonical_json_is_stable_across_repeated_calls` failed once
under load. Investigated rather than re-run — 200,000 randomised values show zero
instability and the call takes 0.16 ms against Hypothesis's 200 ms deadline. It was a
*latency* assertion on a *logical* property, on a machine that had just fitted several
hundred gradient-boosted models. The deadline is now disabled with the reasoning recorded
in `conftest.py`; timing belongs in `make bench`.

---

## Honest summary

**4 / 5 · 4 / 5 · 5 / 5 · 5 / 5**, and 4/5 on craft.

The strongest thing here is that the interesting numbers are the *unflattering* ones and
they are all on the page: recall is 82.90% and not hidden behind the 95.34% match rate;
α=2% breaches on a single split; forecast coverage realises 88.1% against a 90% nominal;
`seasonal_drift` loses to the naive baseline; the cost model's own verdict on an early
fixture was that automation lost to a spreadsheet.

The weakest is that a substantial surface — the dashboard — was built without tests and
needed an adversarial pass to find five real defects, one of which would have thrown on
the first failure a judge triggered. That is fixed, and the fix is a test that was proven
non-vacuous by reintroducing the original bug. But it should not have taken a review.
