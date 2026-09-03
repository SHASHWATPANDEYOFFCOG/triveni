# Threat model

What Triveni is defending, from whom, and what it explicitly does not defend.

The system handles a merchant's books and takes actions that affect them, so the
interesting threats are not "can someone steal the data" — it is synthetic — but **can
someone make the books say something false, and would anyone be able to tell.**

## Assets

| asset | why it matters |
|---|---|
| the reconciled books | a wrong match silently corrupts them and surfaces weeks later |
| the audit log | it is the only evidence of *why* anything was done |
| the auto-post threshold | move it and errors flow into the books unsupervised |
| the merchant's money | the thing everything else is ultimately about |

## Adversaries, in increasing order of capability

### 1. A malformed or hostile source file

**Can do:** supply a bank CSV with unparseable amounts, impossible dates, both a credit
and a debit on one row, or a narration containing instructions aimed at a model.

**Defence.** Every row that fails to canonicalise becomes a typed `corrupt_row` exception
carrying its raw payload, and the batch continues — dropping it would silently lose money
while reporting a clean close. Narrations are scanned for injection **at ingest**, not at
the model boundary, and a match is denied, logged with the patterns it matched, and
flagged for a human regardless of which stage later resolves the row.

**Residual risk.** The injection scanner is pattern-based and a sufficiently novel phrasing
will pass it. That is why it is the *outer* layer: the schema validator, the closed
taxonomy and the fact that the model cannot pick a match all sit behind it.

### 2. A user of the question interface

**Can do:** ask a question that is really an instruction; ask for a figure the data cannot
support; try to reach a table outside the documented views.

**Defence.** The model never writes SQL. It selects from ten hand-written templates and
its parameters are validated — a settlement id must match its shape, a date must parse, a
limit must be 1–200. An intent outside the enum is refused. A topic gate rejects questions
that name nothing the three views model, because answering "how much did we lose to fraud"
with a real cash total is *worse* than refusing: the number is correct and the answer is
about something else. Every numeral in the response is then checked against the rows the
query returned, and an answer containing one that is not there is discarded whole.

**Residual risk.** A question inside the topic vocabulary whose *answer* the templates
compute correctly but which the user misinterprets. The executed SQL is shown for exactly
this reason.

### 3. A compromised or misbehaving model

**Can do:** return schema-invalid output, return a confident wrong classification, attempt
to emit an instruction, or be steered by content in the row it is reading.

**Defence.** One gateway. Schema validation with retry then **abstention** — never
coercion, never repair. Untrusted text wrapped in a content-derived nonce delimiter, so a
narration containing `</source>` cannot close the block early. k-sample agreement measured
on the *decision* rather than the token string; below two-thirds agreement the row abstains
however confident any single sample was. A rupee budget that halts rather than overspends.

**The structural defence matters more than any of those:** the model classifies a residue
row into a closed taxonomy and writes one sentence. It cannot pick a match, cannot compute
an amount, and cannot move a threshold — those are `recon/assign.py`,
`recon/waterfall.py` and `core/conformal.py`, and a test greps the money-path modules to
prove the separation holds.

**Residual risk.** A wrong *classification* within the taxonomy — a `timing` labelled
`short_pay`. This routes to a human either way and is a triage-ordering error, not a
books-affecting one.

### 4. An operator with the API

**Can do:** try to post a match, resolve an exception, or move money through the surface.

**Defence.** `core/guard.py::_post_to_books` is the only function that can affect the
books and it requires an `AppendReceipt` carrying a Merkle inclusion proof under an
Ed25519-signed head. It verifies the proof, the signature, that the audited record names
*this* subject, and that its verdict permits posting. Skipping the log means forging a
signature. Reusing a real receipt for another decision fails the subject check.

Hard caps bind even human approvers: a person may accept a large or uncertain match (a
gate); a person may not exceed a cap (a bound). `ActionClass.MOVE_MONEY` is refused
unconditionally and a test asserts no config field exists that could enable it.

**Residual risk.** An operator with a legitimate approver identity can approve a wrong
match within the caps. That is a trust decision, not a technical one — and the audit log
records who, when and why, which is the correct answer to it.

### 5. An operator with the database file

**This is the interesting one, and the reason the audit log is a Merkle tree.**

**Can do:** bypass the application entirely and rewrite a committed decision on disk.

**Defence, in three layers doing different jobs.**

1. **SQLite triggers** refuse `UPDATE` and `DELETE` on the leaf table. This stops ordinary
   bugs and careless operators. It does *not* stop someone who owns the file.
2. **The Merkle tree** detects any change that gets past layer 1 and **localises** it —
   `verify()` names the exact index whose stored bytes no longer hash to what the tree
   committed to, and, because verification is derived from disk rather than from memory,
   it also *dates* the tampering: heads signed before the altered record still verify, so
   the first size that fails is the point history diverged.
3. **The Ed25519-signed tree head** stops an attacker who rewrites the log *and* recomputes
   the root, because they cannot sign it.

**Residual risk, stated plainly.** In the shipped demo the signing key is derived
deterministically from `TRIVENI_SEED` so a judge gets byte-identical output on any
machine. **An attacker who can read that seed can forge tree heads.** Every head it signs
is stamped `key_kind="demo-deterministic"` so this cannot be mistaken for production, and
`TRIVENI_AUDIT_KEY` supplies a real key through the identical code path. A production
deployment must custody that key outside the application — an HSM or a KMS — and should
publish tree heads externally so an attacker who controls the whole machine still cannot
rewrite history unobserved. That is what Certificate Transparency does with its monitors,
and it is the piece Triveni has the shape for but does not ship.

### 6. Triveni itself, as an adversary

Worth stating because it is the threat a merchant should actually worry about: a system
that quietly marks things reconciled to make its own numbers look good.

**Defence.** Abstention is a first-class outcome with a price attached in the cost model,
so declining is a modelled option rather than a failure. The auto-post threshold is fitted
to a *stated* error bound and the realised error on held-out data is reported beside the
nominal one — including when it breaches. Precision and recall are both published; recall
is 82.90% and it is not hidden behind the 95.34% match rate. The generator refuses to emit
a dataset whose own labels do not balance, so the benchmark cannot be quietly bent either.

## Explicitly out of scope

- **Authentication and authorisation.** There is none. The API is unauthenticated and
  intended to run on localhost for a demo. A deployment needs a real identity layer before
  the approver field means anything.
- **Transport security.** No TLS. Same reason.
- **Multi-tenancy.** One merchant, one dataset, one audit log.
- **Denial of service.** The CP-SAT solver has a time limit but no admission control; a
  hostile dataset engineered for combinatorial blow-up would be slow rather than wrong.
- **Supply chain.** Dependencies are pinned and their APIs were exercised before use
  (ADR 0003), but nothing verifies the packages themselves.

## What would need to change before this touched real money

1. A custodied signing key and externally published tree heads.
2. Authentication, and an approver identity that means something.
3. Re-calibration on the merchant's own labelled data — the conformal bound assumes
   exchangeability, and a threshold fitted on synthetic data transfers to nothing.
4. A human-in-the-loop review of the first weeks of auto-posts regardless of the bound,
   because the bound is a statement about the *calibration distribution*, not about the
   first week of a new merchant.
