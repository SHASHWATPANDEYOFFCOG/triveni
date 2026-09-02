# ADR 0011 - Solve the whole day at once, and price abstention

**Status:** accepted - 2026-09-02

**Context.** Stages 1-3 produce a scored bipartite graph, not an answer. Greedy
pairwise acceptance - take the best pair, then the next - is what most reconciliation
tools do and it is wrong in a specific way: if payment A scores 9.0 against credit X
and 8.8 against Y while payment B scores 8.9 against X only, greedy takes A-X for a
total of 9.0 and strands B. The optimum is A-Y plus B-X, worth 17.7. It does not lose a
little precision; it loses an entire match, and better scoring cannot fix it, because
the mistake is in the acceptance rule.

**Decision.** Two solver layers.

*1:1* uses `scipy.optimize.linear_sum_assignment` with a **dummy no-match column per
row, priced at the accept threshold**. Declining becomes a modelled option competing on
equal terms with every real candidate, so the threshold sits *inside* the optimisation
rather than filtering its output afterwards.

*Many-to-one* uses CP-SAT over the whole day, with "each payment belongs to at most one
settlement" as a hard constraint. Per-settlement subset-sum can produce a plausible
answer for every credit and a jointly impossible one overall.

**Result on the committed seed:** precision 0.9169, recall 0.8153, F1 0.863 (up from
0.516 at M8), **auto-post precision 1.000**, and **zero unexplained rupees** - every
bank credit is either attributed or reported as a typed finding.

## Five errors this milestone produced, each of which satisfied its constraints

That is the lesson worth recording: a solver needs tests about its *objective*, not
only about its feasibility. Every one of these produced a model that solved cleanly.

1. **The tolerance band was inverted** in two of its three call sites, so the solver
   returned settlements where the credit *exceeded* the gross that produced it. Money
   appearing from nowhere, reported as a balanced match. Fixed by a single
   `Tolerance.bounds()` that all three sites share.

2. **The objective telescoped to a constant.** Minimising the *signed* residual sum is
   fixed once every payment is attributed somewhere, so the solver had no preference at
   all between attributions and returned an arbitrary consistent one - 68% of payments
   landed in the wrong settlement with every hard constraint satisfied. The absolute
   residual does not telescope: a misplaced payment makes one residual too large and
   another too small, and both cost.

3. **The settlement calendar was in the candidate set but not in the objective.**
   Amounts are nearly flat between adjacent days, so swapping payments between two
   credits barely moved anything the solver could see. Adding a date-distance penalty
   took precision from 0.60 to 0.77.

4. **Groups were per-row rather than per-relation.** A payment pays an invoice *and*
   settles into a credit. Forcing both claims into one group made Stage 4 absorb Stage
   1's certain invoice links at its own lower confidence, and drove the auto-postable
   share of the entire batch to zero - nothing about those links had become less
   certain. The invariant is now "no row claimed twice **for the same relation**",
   which is the substantive guarantee and strictly stronger where it matters.

5. **A wall-clock solver budget destroyed determinism.** Two runs of the same
   reconciliation returned different attributions, because the timeout landed in a
   different place depending on how busy the machine was. `max_deterministic_time`
   measures the solver's own work units and fixed it.

## Two findings kept rather than tidied away

**More optimisation made the answer worse.** The deterministic budget is deliberately
small (0.5 work units) because measured precision *falls* as it rises: 0.9169 at 0.5,
0.8329 at 2.0. The date-based hint is a very good solution, and given more time the
solver walks away from it toward answers with a better objective value and worse actual
accuracy. That is not a defect in CP-SAT. It is a reminder that the objective is a
proxy for the truth and not the truth, so buying more optimisation of a proxy is not
free.

**`attribution_weight` is a dial, not a constant.** It decides whether attributing a
payment is worth the unexplained money it creates. Writing out the arithmetic for a
bucket whose true residual is `D`, dropping a payment `p < D` changes the objective by
`p*(W-1)`, so at `W = 1` every payment smaller than the residual is dropped and recall
collapses; at `W >= 3` essentially everything is kept. There is no assumption-free
answer, so the default is chosen by measured rupee cost
(`scripts/attribution_sweep.py`) rather than by feel. **Auto-post precision is 1.000 at
every setting on the sweep**: the dial moves how much work reaches a human, not how
much wrong work reaches the books. M12 replaces this in-sample choice with a
distribution-free bound.
