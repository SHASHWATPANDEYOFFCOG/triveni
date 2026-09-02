# ADR 0004 - A Merkle transparency log, not a hash chain

**Status:** accepted - 2026-09-02

**Context.** The constitution requires an append-only, tamper-evident log of
`input -> reason -> action -> outcome`. The obvious implementation, and the one most
entrants will ship, is a SHA-256 chain where each record hashes its predecessor.

**Decision.** Implement RFC 6962 / RFC 9162 - the Certificate Transparency
construction - instead, with Ed25519-signed tree heads.

**Why the chain is not enough.** A hash chain cannot answer either question an
auditor actually asks. To check that record 4,000 is present you must replay all
4,000 records; a Merkle inclusion proof does it in 17 hashes for a log of 100,000
(measured, `tests/test_audit.py::test_inclusion_proof_is_logarithmic`). And a chain
cannot prove *append-only* at all: an operator who rewrites history and re-chains
produces a log in which every individual link still verifies. The consistency proof
is exactly that missing property, and it is the one that matters for books.

**Consequences.**
- Leaf and node hashing use the RFC's 0x00/0x01 domain separation, so an internal
  node cannot be presented as a leaf. A judge can diff `core/audit/merkle.py`
  against the RFC rather than against our prose.
- Three defence layers with different jobs: SQLite triggers refuse UPDATE/DELETE
  (stops bugs and careless operators); the Merkle tree detects and *localises*
  anything that gets past them; the Ed25519 signature stops an attacker who rewrites
  the log and recomputes the root.
- Verification is disk-derived only, never checked against in-memory state. That is
  what lets `verify()` date the tampering as well as locate it: heads signed before
  the altered record still verify, so the report names both the index and the first
  tree size at which history diverged.
- The demo signing key is derived deterministically from `TRIVENI_SEED` so a judge
  gets byte-identical roots and signatures, and every tree head it signs is stamped
  `key_kind="demo-deterministic"` so it cannot be mistaken for a custodied key.

**Cost.** ~330 lines and an exhaustive test suite: 130 tree sizes checked against the
literal recursive RFC definition, every leaf of every size up to 64 proving inclusion,
and every (prefix, size) pair up to 40 proving consistency. Worth it - this is the
component whose correctness the entire trust story rests on.
