# ADR 0013 - One gateway, and what the model is allowed to decide

**Status:** accepted - 2026-09-02

**Context.** The AI-judgment rule is the one most entrants get wrong, and it is only
worth anything if it is *enforced* rather than intended. A comment saying "the LLM only
handles language" is not a boundary.

**Decision.** Nothing in Triveni calls a model directly. There is one door,
`core/llm.py`, and it enforces six rules.

1. **Schema first.** Every call declares a JSON schema. A response that fails it is
   retried once and then **abstains** - never coerced, never repaired, never `eval`'d.
2. **Untrusted input is data.** Source text is wrapped in a **content-derived nonce
   delimiter**, so a narration containing `</source>` cannot close the block early, and
   a rules-based scan runs *before* the call.
3. **A budget that halts.** Tokens and rupees are metered per run; reaching the cap
   abstains rather than spending more.
4. **Offline by default.** `replay` mode serves from cassettes with no key and no
   network. A cassette miss raises loudly - a silent live call would break the
   guarantee it exists to provide.
5. **Abstention is a value, not an exception.** `LLMOutcome.abstained` carries a
   reason; there is no path where a failed call becomes a confident answer.
6. **k-sample agreement on the decision.** The residue prompt runs three times and
   agreement is measured on the *classification*, not the token string - a model that
   says "timing difference" and "settlement timing" has agreed about what to do. Below
   two-thirds agreement the row abstains however confident any single sample was. This
   is the semantic-entropy idea (Farquhar et al., Nature 2024) applied to decisions.

**What the model decides:** which of sixteen categories a residue row belongs to, and
one sentence of why. **What it never decides:** which records match, how much a fee is,
or where a threshold goes. Those are `recon/assign.py`, `recon/waterfall.py` and
`core/conformal.py`, and a test greps the money-path modules to prove the separation
holds. Measured on the seed: **1 model call is required across 536 rows** for narration
parsing, because 95% of narrations are a *format*, and a format is a regex.

**Scan where untrusted data enters, not only where it reaches a model.** The first
implementation scanned at the model boundary, which meant the seed's attacking
narration was resolved by Stage 4 and the denial never appeared - the defence worked
and was invisible. Scanning at ingest means the attack is denied, logged with its
matched patterns as evidence, and flagged for a human *regardless* of which stage
resolves the row.

**On the shipped cassettes - stated plainly.** They are **synthetic fixtures, not
recordings of a live model.** This build has no API key, and writing invented outputs
into a file labelled "recorded" would be exactly the dishonesty rule A.6 forbids. Every
entry is stamped `"source": "synthetic-fixture"`, the gateway says so in its report, and
`TRIVENI_LLM_MODE=record` with a key writes real ones through the same code path.

Two properties keep the fixtures honest. They are generated **through the real call
path** rather than a reimplementation of it - reproducing the call site by hand produced
keys that differed by a whitespace and missed every time. And the stand-in that
produces them sees **only what the model would see**, never the ground truth; a fixture
derived from the labels would make the residue stage look perfect while proving
nothing.

**`scripts/redteam.py` - 13 cases, all passing.** Prompt injection denied with
evidence; delimiter escape defeated by the nonce; schema violation abstains; budget
exhaustion abstains; model switched off entirely leaves matching unchanged at 270
matches; cassette miss raises; audit tampering named at the exact index; a forged
receipt cannot post; money movement refused with no config field that could enable it;
kill switch records the operator's reason first; corrupt rows kept as evidence;
unexplainable settlements abstain with a stated reason; duplicates reported with both
copies retained.
