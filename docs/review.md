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
- Clean clone → `make setup && make demo` → the full nine-beat narrative in **~27s**,
  no keys, no network. Verified by deleting `data/generated/`, `metrics.json` and
  `.triveni/audit.db` and re-running.

  **This figure was wrong here until now, and the correction is the point.** The
  document claimed 8.4s. Re-timed on an idle machine across two consecutive runs it is
  26.8s and 28.3s. `scripts/demo.py` has not been touched since the number was written,
  so the demo grew past its measurement over several milestones and the document did
  not follow — which is precisely the failure mode that `scripts/gen_web_metrics.py`
  now prevents on the landing page, and that nothing prevents in prose. The stated
  budget in `tasks.py` is <90s and that is still comfortably met.
- **872 tests**, `mypy --strict` clean on `core/` and `recon/`, `ruff` clean.
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

> Was 4, revised down to 3 when the dashboard turned out to be unusable in a browser
> while 74 static tests stayed green, and now back to 4 after a full design-system
> pass. The bug is fixed, the gap that hid it is named in `docs/limitations.md`, and
> the surface is substantially larger than it was. It is not a 5, for the reasons at
> the end of this section.

**The design system.** 190+ tokens in one file - colour, type, space, radius, elevation,
blur, motion, z-index, breakpoints - in two complete themes, with every component built
against them ([ADR 0019](adr/0019-a-design-system-without-a-framework.md)). A test
asserts every `var()` in the product resolves, and another asserts the six row states
stay on their own hue ramps, so the brand blue can never start meaning "healthy".

**A landing page that argues the case**: the netted-settlement problem, the three loops,
the five non-negotiables with the file that enforces each, the six-stage ladder, the
where-the-model-is-not table, and the measured numbers. **No figure on that page is
typed by hand** — `scripts/gen_web_metrics.py` generates them from `metrics.json`, and
two tests fail the build if the page drifts from the measurement or hard-codes one.

**A 3D hero that carries the argument**: three labelled streams converging on one
settlement core, which is what a close *is*. Written as a 120-line perspective engine on
Canvas rather than pulled from Three.js, and the deciding reason was testability - the
projection maths is **fifteen assertions runnable under plain Node**, covering exactly
the errors that still draw something plausible when they are wrong.

**The accessibility finding worth reporting.** Because the palette is tokens, a test can
flatten each theme, composite the translucent chips over their surface and compute WCAG
ratios. It immediately found that **five of the six row states failed AA for small text
in the light theme** - between 2.86:1 and 4.44:1 against a 4.5:1 requirement, on the most
semantically loaded colour in the product. Every one now clears it with margin, the
worst at 5.02:1. That defect had been in the product since M16 and no amount of looking
at it would have found it.

**Why not 5.** Still no Lighthouse run and still no cross-browser testing, so neither is
claimed. And the deeper gap is unchanged: **every test here is static.** They prove the
source is self-consistent - that ratios computed from tokens are sound, that every
identifier resolves - and none of them proves the browser agrees. That is the same class
of blindness that let an undismissable modal ship, and it is closed only by a headless
browser this project does not have.


**The rest of the surface.** A landing page and eight screens, **zero dependencies, zero
build step**, served by the API process ([ADR 0018](adr/0018-a-zero-build-dashboard.md),
[ADR 0019](adr/0019-a-design-system-without-a-framework.md)). Both themes as complete
token sets with `[data-theme]` beating `prefers-color-scheme` in both directions; money
always tabular-nums with Indian grouping; every animation a no-op under reduced motion,
guarded centrally in `tokens.css` so it cannot be forgotten per-component; one shared
dialog module so Escape, the focus trap, the scrim click and the focus restore are
implemented once rather than four times with a different subset each; the rail becomes a
drawer and tables reflow to labelled cards below 64rem; responsive to 390px.

Two earlier defects on this surface are worth keeping on the record, because both were
found by an adversarial pass rather than by looking: an inconsistent `balanced` default
that could stamp a green "balanced to ₹0" seal over books that were short
(`web/js/screens/waterfall.js:331`, now `=== true`), and a confidence band whose lower
edge was drawn time-reversed.

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

---

## What running it live found

The five findings above came from *reading* the code. These came from opening the
dashboard in a browser and using it, which is a different exercise and a more
embarrassing one — the first thing it found was that the product did not work at all.

### 1 · The dashboard was unusable on load, and every test passed

`el.hidden = true` depends on `[hidden] { display: none }`, which lives in the
**user-agent** stylesheet. Author rules beat the UA stylesheet by *origin* — specificity
never enters into it — so `.help-overlay { display: grid }` in `screens.css` silently
won. That overlay is `position: fixed; inset: 0; z-index: 100`.

The consequence: the keyboard-help dialog covered all eight screens from first paint,
and neither <kbd>Esc</kbd> nor its own close button could dismiss it — both set
`.hidden`, which by then meant nothing. The same trap hit `.btn { display: inline-flex }`
on the audit screen, which showed "tamper" and "restore" simultaneously.

Fixed with one global `[hidden] { display: none !important }` in `web/styles/app.css`,
where `!important` is load-bearing rather than lazy: it has to beat component rules that
do not exist yet, in a stylesheet loaded later. `tests/test_dashboard.py` asserts the
rule survives, and the assertion was proven non-vacuous by deleting the rule and
watching it fail.

**The lesson is the uncomfortable part.** `tests/test_dashboard.py` was written in
response to the last review's finding that `web/` had no tests. It has 74 of them. All
74 passed while the dashboard was unusable, because every one is a *static* test — they
read the source and check what it says, and no static analysis of correct-looking CSS
and correct-looking JS reveals a cascade-origin conflict between two files. The tests
were not wrong; they were the wrong *kind*, and the previous review's claim that the
`web/` gap was closed was too confident. It closed the undefined-identifier class and
left the "does it actually render" class wide open.

### 2 · The page blocked ten seconds on a call it made twice

`/close` took ~19s and the dashboard awaited it before first paint, so a cold load showed
skeletons and read as dead. Two causes, both measured:

- `_tool_close_books` called `self._result()` (cached) **and** `evaluate_pipeline()`,
  which called `reconcile()` again independently — so every request paid the ~10s twice,
  and the α slider paid it again per position.
- Nothing was memoised per α, and nothing was warmed at startup.

Fixed by threading the existing reconciliation into `evaluate_pipeline()`, memoising the
report per α, warming the reconciliation on a background thread at startup, and splitting
the page load into two waves so the shell paints from `/health` alone. Measured, same
machine:

| | before | after |
|---|---|---|
| first paint | ~19,000 ms | **22 ms** |
| full data on screen | ~19,000 ms | **1,745 ms** |
| dragging α to a new value | ~19,000 ms | **~200 ms** |
| returning to a seen α | ~19,000 ms | **4 ms** |

The ten seconds themselves are real and are not hidden: a banner says the books are
closing and why the global solver costs what it costs.

### 3 · A determinism flake, and a wrong first diagnosis worth recording

`test_the_solver_is_deterministic` failed in a full-suite run and passed in isolation.

My first diagnosis was wrong, and the way it was wrong is the point. `recon/subsetsum.py`
set `max_time_in_seconds` — a wall-clock budget, on a parameter already *named*
`deterministic_budget`-style and documented as work units, and the exact bug that had
already been fixed in `recon/assign.py`. It looked like an open-and-shut cause. It was
not: instrumenting the pipeline showed `cp_sat_subset` is called **zero** times on the
536-row seed *and* zero times on the full 5,481-row set, because no settlement bundle
exceeds `MITM_LIMIT = 34` items. It is reached only by its own unit tests. The fix is
kept because the inconsistency was real, but it fixed nothing, and reporting it as the
cause would have been a fabricated causal claim.

The actual cause: the LLM gateway's ₹25 spend cap is **process-wide by design**. Across a
full suite run it is partly spent by the time this test runs, and it can fall *between*
the test's two `reconcile()` calls — so the first gets model answers and the second
abstains. The signature said so and I read past it: the **match** lists were identical
and only the **exception** lists diverged, which is escalation abstaining, not a solver
wobbling.

That signature turned out to be a property worth owning rather than a nuisance. Draining
the budget deliberately changes the exception list — more rows route to a human — and
leaves the match list byte-identical. Budget exhaustion costs *coverage*, never
*correctness*, which is the only direction a finance system may degrade in. It is now
asserted directly by
`test_running_out_of_llm_budget_never_changes_a_money_decision`, and
`test_the_solver_is_deterministic` resets the gateway so it measures the solver its name
refers to.

`make eval`'s byte-identical guarantee was never at risk: it runs in a fresh process with
a fresh budget. But the guarantee is narrower than it sounded, and now says so.

### What this run costs the scores

Craft drops from 4 to **3**. A dashboard that cannot be used until a one-line CSS fix is
not a 4, and the gap was not caught by 74 tests written specifically to catch dashboard
bugs.

Build quality stays at **4** — the API and pipeline fixes are measured, and the
determinism investigation ended with two sharper tests rather than a weakened one — but
the honest note is that the first diagnosis was confidently wrong for twenty minutes and
was only caught by instrumenting instead of assuming.

The standing lesson: **static tests prove what the source says, not what the browser
does.** Closing that gap properly needs a headless browser, which
`docs/limitations.md` now records as absent rather than implying otherwise.

---

## Honest summary

**4 / 5 · 4 / 5 · 5 / 5 · 5 / 5**, and 4/5 on craft.

The strongest thing here is that the interesting numbers are the *unflattering* ones and
they are all on the page: recall is 82.90% and not hidden behind the 95.34% match rate;
α=2% breaches on a single split; forecast coverage realises 88.1% against a 90% nominal;
`seasonal_drift` loses to the naive baseline; the cost model's own verdict on an early
fixture was that automation lost to a spreadsheet.

The weakest is the dashboard, twice over. It was built without tests and needed an
adversarial pass to find five real defects. Then 74 tests were written in response — and
all 74 passed while a modal covered every screen from first paint and could not be
dismissed, because they were static tests and the bug was a CSS cascade-origin conflict
between two files. Reading the source proved the source was consistent. It was not until
the page was opened in a browser that anyone learned it did not work.

That is the finding I would most want a judge to know, because the previous version of
this document claimed the `web/` testing gap was closed. It was not. It was narrowed —
the undefined-identifier class is genuinely caught now — and the class that actually
takes a product down was still wide open. The correct fix is a headless browser, which
this project does not have and `docs/limitations.md` now says so plainly rather than
letting a test count imply coverage it does not have.
