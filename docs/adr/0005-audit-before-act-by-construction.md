# ADR 0005 - "Audit before act" is enforced by a receipt, not a convention

**Status:** accepted - 2026-09-02

**Context.** Invariant D.1.5 requires that posting to the books without an audit
record be *impossible by construction*. A private method named `_post`, a code
comment, or a review checklist do not achieve that - each is a convention that the
next contributor can break without noticing.

**Decision.** The only function permitted to effect a books-affecting change,
`core/guard.py::_post_to_books`, takes an `AppendReceipt` as a required argument and
verifies four things before invoking the caller's side effect: the Merkle inclusion
proof holds under the tree head, the tree head carries a valid Ed25519 signature, the
audited record names *this* subject, and its verdict permits posting.

**Why this is different from a convention.** A caller who skipped the log cannot
fabricate a receipt - doing so requires forging a signature over a root. A caller who
holds a *real* receipt for some other decision cannot reuse it, because the subject is
checked. The ordering is therefore a precondition cryptography enforces, not a habit.

**Consequences.**
- Denials are logged too. A log that records only what was done cannot answer "why
  did nothing happen on the 14th?", which is precisely the auditor's question.
- A side effect that fails *after* approval writes a second `.failed` record rather
  than being swallowed, so the log never implies an action that did not land.
- `evaluate()` is total: it returns a `Decision` for every input including adversarial
  ones, proved by a 300-example property test. This mattered in practice - the first
  implementation raised `CurrencyMismatch` on a USD amount instead of denying it, and
  a policy engine that can be crashed by hostile input is one that can be bypassed by
  crashing it.
- Hard caps bind even human approvers. A person may accept a large or uncertain match
  (a gate); a person may not exceed a cap (a bound). That distinction is the whole
  design.
