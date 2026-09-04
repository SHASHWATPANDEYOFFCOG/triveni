# Video script — full walkthrough

**~7:00 full. A 4:00 cut is marked with ✂ — drop those blocks in the order listed at the
bottom.**

Every figure spoken below is on screen at the moment you say it, and reproducible by the
command in the block. Nothing here is a slide of claims.

> **The one rule for this recording:** if a number is not on screen, do not say it. If a
> number *is* on screen and it is unflattering, say it anyway — that is the whole
> argument of this project.

---

## Setup before you record

```bash
make setup                 # once
make eval                  # writes metrics.json + the landing page's figures
rm -f .triveni/audit.db    # so the audit screen starts from a clean log
make demo                  # writes a fresh audit log (~27s)
make run                   # leave running on :8000
```

Then, **before** hitting record:

1. Open `http://127.0.0.1:8000/app/` **once** and let it finish loading. The first close
   is a cold reconciliation and takes a few seconds; after that it is cached and every
   screen is instant. Recording the cold load makes the product look slow for a reason
   that has nothing to do with the product.
2. Dark theme. Reduce-motion **off**.
3. Browser at ~1440px wide so the sidebar is visible (below 1024px it collapses to a
   drawer and you lose the 8-screen overview).
4. Terminal in a second window, large font, dark.
5. Press **"Got it"** on the first screen's instruction strip if you do not want it in
   shot — or leave it, it explains the screen for you.

---

## 0:00 – 0:35 · The problem, in rupees ✂

**Show:** terminal, full width.

```bash
python -m scripts.size_the_problem
```

**Say:**

> An Indian SMB on a payment gateway doesn't get one line per sale. They get one netted
> settlement a day. Between the MDR, the eighteen percent GST *on that MDR*, the
> point-one percent TDS under 194-O, refunds and a T+2 offset — the money that lands
> almost never equals the books.
>
> Somebody reconciles that by hand. Every day.

Point at the band as it prints.

> Fourteen thousand to eighty-three lakh a year, per merchant. That band is wide because
> every input is an assumption, and the script labels each one. I'd rather show you a
> range you can argue with than a round number you can't check.

---

## 0:35 – 1:15 · The front door

**Show:** browser → `http://127.0.0.1:8000/app/landing.html`

Let the hero settle. Move the cursor slowly across the 3D scene so it leans.

**Say:**

> Three ledgers — payments, the bank statement, the internal ledger — converging on one
> settlement. That's not a stock animation. The three streams are the three sources, and
> they meet at a single core, because that's what closing a day actually is.

Scroll to **Where the time goes** (the dashboard preview section).

> This preview is live. It's calling the running API and drawing the real per-stage
> timings.

Scroll to the **numbers** section.

> Every figure on this page is generated from `metrics.json` by a script, and two tests
> fail the build if the page drifts from the measurement or if anyone types a number
> into the HTML by hand.

Point at **Recall** and **Exceptions raised** — they are styled in amber, same size as
the rest.

> Recall is eighty-two point nine percent. It's on the page at the same size as the
> ninety-five percent match rate, because a dashboard that shrinks its bad numbers is
> lying with typography.

Click **Open the dashboard**.

---

## 1:15 – 2:00 · Screen 1 · Overview — the close

**Show:** the dashboard lands on **Confluence**. Four KPI tiles count up.

**Say:**

> Five hundred and thirty-six rows across three ledgers. Two hundred and seventy match
> groups. Eighty-seven rows the system refused to decide on its own.

Point at the ring on the right.

> The ring is auto-post coverage — seventy-two point eight percent posted with no human.
> The amber tick on the ring is the threshold those postings had to clear. That threshold
> isn't a round number somebody liked; it's fitted, and I'll show you where it comes from
> in a moment.

**Hover across the "Where the time goes" bar.** Stop on the two widest segments.

> Here's the thing I'd want to know as an engineer. Fellegi–Sunter and the global
> assignment are about ninety-six percent of the whole close.

Point at the on-screen total.

> That's the honest cost of solving the entire day at once instead of greedily. Greedy
> matching is fast and it loses matches — it takes the best-looking pair and strands the
> row that needed it. This solves the assignment globally, and it pays for that in
> seconds.

Point at the **exception bars** (bottom left).

> And these are typed, not a pile. Sixty-eight payments with no matching bank credit, ten
> duplicates, five missing in the ledger, two corrupt rows.

Press **Close the books** — the stage log fills.

> Every stage reports what it did over server-sent events.

> ⚠️ **If you already loaded the page, this replays instantly from cache.** Either say
> "that's cached — the cold run takes a few seconds", or restart the server first if you
> want the progressive fill on camera. Don't imply it's cold when it isn't.

---

## 2:00 – 2:30 · Screen 2 · Stages — why so few rows reach a model

**Show:** press `2`. Click along the ladder segments.

**Say:**

> Five staged passes, cheapest and most certain first. Deterministic keys — UTR and RRN —
> take two hundred and fifty-three matches for free. Then blocking, then probabilistic
> record linkage, then the global solver.
>
> Each stage only ever sees what the one before it couldn't match. That's the reason the
> next number is what it is.

Point at the last rung.

> Thirteen rows out of five hundred and thirty-six ever reach a language model. Two point
> four percent. Thirty-nine calls, because ambiguous rows get sampled more than once.
>
> Both numbers matter. I used to say "one model call" — that counted only narration
> parsing and it was flattering and wrong. Say two point four percent of rows, or say
> thirteen rows and thirty-nine calls. Never just the small one.

---

## 2:30 – 3:15 · Screen 3 · α — the dial that matters ✂ (never cut)

**Show:** press `3`.

**Say:**

> This is the only dial in the product. Alpha is how often the system is allowed to post
> something wrong without a human looking.

Press the **1%** preset.

> One percent. The threshold fits to point nine-five-four-eight, coverage lands at
> seventy-two point eight, and the realised error on held-out data is zero.

Press **2%**, then **5%**. Let the tiles move together.

> Loosen it and coverage goes up — and so does the cost of being wrong. They move
> together because they're the same trade.

Point at the curve.

> And note it's a step function, not a smooth line. Each point is a separately calibrated
> threshold, not an interpolation.

Scroll to the caveat box.

> This is split conformal prediction with the finite-sample correction, so the bound is a
> bound and not an average. It assumes exchangeability between the calibration slice and
> the rows it's applied to — and reconciliation data breaks that routinely. A new payment
> method has fees the model has never seen. Month-end isn't exchangeable with mid-month.
>
> So it's a well-founded operating point that has to be re-calibrated when the data
> moves. It is not a standing promise, and the screen says so rather than me saying it.

---

## 3:15 – 3:45 · Screen 4 · Triage — what a refusal looks like

**Show:** press `4`. Press `j` a few times, then `e` on a row.

**Say:**

> Eighty-seven exceptions, each with a type, a severity, its evidence, and a sentence
> saying why the system stopped.

Read one `abstained_because` aloud.

> That's the part I care about. When it doesn't know, it says which of the two candidates
> it couldn't separate and why — it doesn't pick one and hope.
>
> Accept or reject with `a` and `r`. Both write to the audit log, which is the next
> screen.

---

## 3:45 – 4:15 · Screen 5 · Settlement ✂ (never cut)

**Show:** press `5`. Pick a settlement.

**Say:**

> One netted credit arrives. "Fees" is not an answer. So: gross captured, minus MDR,
> minus eighteen percent GST **on that fee — not on the sale**, minus point-one percent
> TDS under 194-O, minus refunds that settled in the same cycle.

Point at the seal.

> Sixteen of sixteen settlements balance to zero rupees. Not "approximately" — integer
> paise, exact.
>
> And the rates aren't hard-coded. They're fitted from the data by robust non-negative
> least squares. A hallucinated basis point is a real loss, so that number is never
> allowed to come from a model.

---

## 4:15 – 4:45 · Screen 6 · Forecast

**Show:** press `6`.

**Say:**

> Cash landing per day on the real Indian bank calendar — including the second and fourth
> Saturday, when banks are closed.

Point at the band, not the line.

> Read the band, not the line. The line is a median; half the time you land below it.

Point at the shortfall alert.

> So the alert fires on the **lower bound**. Alerting on a point forecast means alerting
> at a fifty percent chance of being short, and staying quiet exactly when the band is
> widest — which is when you most need telling.

Open the model ladder.

> Scored on MASE and pinball loss, never MAPE — MAPE is undefined on a zero-cash day and
> this series is full of them. And the rungs that lose to the one-line baseline are
> published as losing. A ladder where the fanciest model always wins is a ladder nobody
> should believe.

---

## 4:45 – 5:30 · Screen 7 · Audit ✂ (never cut)

**Show:** press `7`.

**Say:**

> Every decision is committed to an append-only Merkle log — the RFC 6962 construction
> that secures the web PKI, applied to a financial decision log.

Point at the five checks, all green.

> It proves a record is present in log-n hashes, and it proves the log was *appended to*
> rather than rewritten. A hash chain can't do the second one at all: an operator who
> rewrites history and re-chains produces a log where every individual link still
> verifies.

Switch to the **terminal**:

```bash
python -m core.audit.verify --tamper
```

**Say, pointing at the output:**

> Corrupt one committed record, going straight at the database and around the
> append-only triggers.
>
> It fails at the exact index. And look at this line — the first head that no longer
> verifies. The heads signed *before* the tampering still pass. So it doesn't just locate
> the change, it **dates** it.

Run it again with no flag:

```bash
python -m core.audit.verify
```

> And it healed itself. Demonstrating a defence shouldn't leave the thing it defends
> broken.

---

## 5:30 – 6:15 · Screen 8 · Report, and the boundary

**Show:** press `8`. Scroll through.

**Say:**

> Everything measured, in one page, split into what was measured and what was assumed.

Point at the assumed block.

> The six cost parameters — an analyst's loaded hourly cost, minutes to triage, minutes
> to unwind a false match — are **stated illustrative assumptions**, not sourced findings.
> Swap them and every rupee figure moves. The screen says that; I'm not going to quote
> you a savings number as if it were measured.

Switch to the terminal:

```bash
curl -s localhost:8000/boundaries | python -m json.tool | head -20
```

**Say:**

> Ten read tools on the Razorpay rail. Zero write tools. Eleven write tools named
> explicitly and refused — by an allowlist checked before any transport, by an
> import-time assertion that they can never appear in it, and by read-only on the server.
>
> Three independent mechanisms, because that's the one boundary where being wrong is
> unrecoverable. Triveni proposes. There is no transfer capability anywhere in the system
> to reach.

---

## 6:15 – 7:00 · What it is, and what it isn't

**Show:** terminal.

```bash
make redteam
```

**Say:**

> Thirteen adversarial cases. Prompt injection in a bank narration is denied at
> **ingest** — before anything reaches a model, not at the model boundary.

Then:

```bash
TRIVENI_LLM_MODE=off make demo
```

> Switch the model off entirely and matching is unchanged. The books still close. The
> rows that needed language abstain into typed exceptions for a human. That's the test of
> whether a system's intelligence is load-bearing or decorative.

**Close on:**

> Eight hundred and eighty-four tests. `mypy --strict` clean. An AST lint that proves no
> float can enter a money path. `make eval` twice gives byte-identical output.
>
> And what it isn't. The data is synthetic. The LLM cassettes are synthetic fixtures and
> every entry says so — I had no API key and I wasn't going to write invented outputs
> into a file labelled "recorded". Recall is eighty-three percent, not ninety-nine.
> Sixty-eight payments the solver couldn't attribute became exceptions rather than being
> forced into a group.
>
> One command. No keys, no network, no Razorpay account:

```bash
make demo
```

> That's Triveni.

---

## If you need to cut to 4:00

Drop in this order. The four money-shots stay:

1. α moving coverage, error and cost together **(never cut)**
2. the waterfall landing on *balanced to ₹0* **(never cut)**
3. the tamper failing at the exact index **and dating it** **(never cut)**
4. the model switched off and matching unchanged **(never cut)**
5. — then drop: the landing page (0:35–1:15)
6. — then: the problem sizing (0:00–0:35)
7. — then: the stage ladder (2:00–2:30)
8. — then: triage (3:15–3:45)
9. — then: the forecast (4:15–4:45)

## Do not say

- **"AI-powered reconciliation."** The model does the *language*. The matching is a
  solver. Say "an AI finance controller that knows where not to use AI."
- **"One model call."** It is 13 rows and 39 calls. Say 2.4% of rows, or say both.
- **"99% accurate."** Precision is 98.06%, recall is 82.90%. Say both or say neither.
- **"It saves you ₹X."** The cost parameters are illustrative assumptions. Say so in the
  same breath, or don't say the number.
- **"Watch it reconcile live"** over a cached replay. Either restart the server or say
  it's cached.
- **"Real merchant data."** It is synthetic and generated by `data/gen.py`.
- Anything implying the cassettes came from a live model.
- **"Fully tested."** 884 tests, none of which open a browser. The UI is covered by
  static analysis only, and `docs/limitations.md` says so.

## Numbers you may quote (all from `metrics.json`)

| figure | value |
|---|---|
| rows reconciled | 536 |
| match groups | 270 |
| match rate | 95.34% |
| precision / recall | 98.06% / 82.90% |
| auto-post coverage | 72.82% |
| auto-post precision | 100.00% |
| fitted threshold | 0.9548 |
| realised error vs bound | 0.00% vs 1.00% |
| exceptions raised | 87 |
| rows reaching a model | 13 (2.43%), 39 calls |
| settlements balancing | 16 / 16 to ₹0 |
| corrupt rows survived | 2 |
| tests | 884 |

Re-run `make eval` and re-read this table before recording. If a figure here disagrees
with the screen, **the screen is right** — this table is prose and prose drifts, which is
exactly the failure `scripts/gen_web_metrics.py` exists to prevent on the landing page.
