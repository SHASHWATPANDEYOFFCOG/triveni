# ADR 0006 - Errors are priced in rupees, and the price is asymmetric

**Status:** accepted - 2026-09-02

**Context.** Accuracy metrics cannot tell you where to put a threshold, because
Triveni's two failure modes have wildly different consequences. Optimising F1 would
implicitly assume they cost the same.

**Decision.** `core/costmodel.py` prices an outcome mix. A **false match** costs the
labour to find and unwind it weeks later plus a share of the exposed value that is
never recovered. A **false non-match** costs a few minutes of triage. An **abstention**
costs the same few minutes and is therefore a *modelled option with a price*, not a
failure. A correct auto-post costs nothing, because nobody looked at it - which is the
entire economic case, and also why the false-match cost has to be modelled honestly:
if auto-posting had no downside, the optimal policy would be to auto-post everything.

**The number that matters.** `break_even_false_matches()` returns how many false
matches are affordable before automation costs more than a spreadsheet. On the M4
harness fixture that budget is **3** and the fixture makes **6**, so `saved_vs_manual`
comes out **negative**. We report that rather than retuning the fixture: it is the
model working. A false match costs roughly 15x a missed one, so automation only pays
once the false-match rate is driven very low - which is the entire argument for the
conformal threshold at M12 and for the alpha slider in the UI.

**Honesty.** Every parameter is an `Assumption` carrying its own provenance string,
and each default is labelled `illustrative` because we have not sourced it from a
published study. The type refuses an empty source, the renderer lists every
illustrative parameter under the table, and `metrics.json` carries them all. An
assumption that announces itself is honest; one dressed as a finding is not.

**Reproducibility.** `metrics.json` carries no timestamp, by design - a report that
records when it ran cannot be byte-compared against itself. Provenance is the seed
plus a digest of the source that produced the numbers, which is more honest than a
git SHA because it moves when someone edits a threshold without committing.
