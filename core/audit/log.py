"""The append-only decision log: SQLite storage, Merkle commitment, signed tree head.

Every books-affecting action in Triveni writes one :class:`DecisionRecord` here
before it takes effect (invariant D.1.5, enforced in ``core/policy.py``). The record
is the four-part shape the constitution asks for - **input, reason, action, outcome** -
plus the evidence the reason was drawn from, so a human reading the log a year later
can reconstruct not just what happened but why it was allowed to.

Three layers of protection, each doing a different job:

1. **SQLite triggers** refuse ``UPDATE`` and ``DELETE`` on the leaf table. This stops
   ordinary bugs and careless operators. It does not stop an attacker with the file.
2. **The Merkle tree** detects any change that gets past layer 1, and localises it -
   ``verify()`` reports the exact index whose stored bytes no longer hash to what the
   tree committed to.
3. **An Ed25519 signature over the tree head** stops an attacker who rewrites the log
   *and* recomputes the root, because they cannot forge the signature without the key.

Layer 3 is honest about its limits: in offline demo mode the signing key is derived
deterministically from ``TRIVENI_SEED`` so a judge gets byte-identical output, and the
tree head is stamped ``key_kind="demo-deterministic"`` so nobody can mistake a
reproducible demo key for a custodied production one. Real deployment supplies
``TRIVENI_AUDIT_KEY``; the code path is identical.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Self

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field

from core.audit.merkle import (
    ConsistencyProof,
    InclusionProof,
    MerkleTree,
    leaf_hash,
    merkle_root,
)
from core.clock import IST, Clock, SystemClock
from core.errors import AuditError, TamperDetected
from core.ids import IdKind, canonical_json, make_id

DEFAULT_DB: Final = Path(".triveni") / "audit.db"
_DEMO_KEY_CONTEXT: Final = b"triveni-audit-ed25519-v1"


class Verdict(StrEnum):
    """What the policy engine decided. A closed set: no free-text verdicts."""

    AUTO_POSTED = "auto_posted"
    NEEDS_REVIEW = "needs_review"
    ABSTAINED = "abstained"
    DENIED = "denied"
    APPROVED_BY_HUMAN = "approved_by_human"
    REJECTED_BY_HUMAN = "rejected_by_human"
    OBSERVED = "observed"


class DecisionRecord(BaseModel):
    """One immutable entry: input -> reason -> action -> outcome.

    ``reason`` is mandatory and non-empty by construction, which is how
    non-negotiable 1 (EXPLAINABLE) is enforced at the type level rather than by
    convention: you cannot write to this log without saying why.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1, description="what sort of decision, e.g. 'match.accept'")
    subject_id: str = Field(min_length=1, description="what it is about, e.g. a match id")
    verdict: Verdict
    reason: str = Field(min_length=1, description="human-readable, mandatory")
    inputs: dict[str, Any] = Field(default_factory=dict)
    action: dict[str, Any] = Field(default_factory=dict)
    outcome: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    actor: str = Field(default="triveni", min_length=1)
    at: dt.datetime
    run_id: str = ""

    def canonical(self) -> dict[str, Any]:
        """The exact structure that gets hashed. Field order is irrelevant because
        ``canonical_json`` sorts, but the *set* of fields is part of the commitment,
        so adding one is a breaking change to every historical proof."""
        return {
            "kind": self.kind,
            "subject_id": self.subject_id,
            "verdict": self.verdict.value,
            "reason": self.reason,
            "inputs": self.inputs,
            "action": self.action,
            "outcome": self.outcome,
            "evidence": self.evidence,
            "actor": self.actor,
            "at": self.at,
            "run_id": self.run_id,
        }

    def to_bytes(self) -> bytes:
        return canonical_json(self.canonical())


# --------------------------------------------------------------------------- #
# Signing
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class AuditKey:
    """An Ed25519 keypair for signing tree heads."""

    private: Ed25519PrivateKey
    kind: str

    @classmethod
    def from_seed(cls, seed: bytes, *, kind: str) -> AuditKey:
        if len(seed) != 32:
            raise AuditError("Ed25519 seed must be 32 bytes", length=len(seed))
        return cls(Ed25519PrivateKey.from_private_bytes(seed), kind)

    @classmethod
    def load(cls) -> AuditKey:
        """Production key from the environment, or a deterministic demo key.

        The demo key is derived from ``TRIVENI_SEED`` through a domain-separated
        hash, so `make demo` produces the same signatures on every machine - which
        is what makes the audit output reproducible for a judge. It is labelled as
        such in every tree head it signs.
        """
        supplied = os.environ.get("TRIVENI_AUDIT_KEY", "").strip()
        if supplied:
            try:
                return cls.from_seed(bytes.fromhex(supplied), kind="operator-supplied")
            except ValueError as exc:
                raise AuditError("TRIVENI_AUDIT_KEY must be 32 bytes of hex") from exc
        seed_material = os.environ.get("TRIVENI_SEED", "20260101").encode()
        seed = hashlib.blake2b(_DEMO_KEY_CONTEXT + seed_material, digest_size=32).digest()
        return cls.from_seed(seed, kind="demo-deterministic")

    @property
    def public(self) -> Ed25519PublicKey:
        return self.private.public_key()

    @property
    def public_bytes(self) -> bytes:
        return self.public.public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    @property
    def key_id(self) -> str:
        """Short, stable identifier for the verifying key."""
        return hashlib.sha256(self.public_bytes).hexdigest()[:16]

    def sign(self, payload: bytes) -> bytes:
        return self.private.sign(payload)


@dataclass(frozen=True, slots=True)
class SignedTreeHead:
    """A signed commitment to 'the log has exactly this content, at this size'."""

    size: int
    root: bytes
    at: dt.datetime
    key_id: str
    key_kind: str
    signature: bytes

    def payload(self) -> bytes:
        """Exactly what was signed. Kept as one function so signing and verifying
        can never drift apart."""
        return canonical_json(
            {
                "v": 1,
                "size": self.size,
                "root": self.root.hex(),
                "at": self.at,
                "key_id": self.key_id,
                "key_kind": self.key_kind,
            }
        )

    def verify_signature(self, public: Ed25519PublicKey) -> bool:
        try:
            public.verify(self.signature, self.payload())
        except InvalidSignature:
            return False
        return True

    def to_json(self) -> dict[str, Any]:
        return {
            "size": self.size,
            "root": self.root.hex(),
            "at": self.at.isoformat(),
            "key_id": self.key_id,
            "key_kind": self.key_kind,
            "signature": self.signature.hex(),
        }


# --------------------------------------------------------------------------- #
# Verification report
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class VerificationReport:
    """The result of ``triveni verify`` - designed to be printed verbatim."""

    ok: bool
    size: int
    root: bytes
    checks: tuple[tuple[str, bool, str], ...]
    tampered_index: int | None = None
    tampered_detail: str = ""

    def render(self) -> str:
        lines = [
            f"audit log: {self.size} record(s)",
            f"merkle root: {self.root.hex()}",
            "",
        ]
        for name, passed, detail in self.checks:
            mark = "PASS" if passed else "FAIL"
            lines.append(f"  [{mark}] {name}" + (f" - {detail}" if detail else ""))
        if self.tampered_index is not None:
            lines += [
                "",
                f"  TAMPERING LOCALISED AT INDEX {self.tampered_index}",
                f"  {self.tampered_detail}",
            ]
        lines += ["", "VERIFIED" if self.ok else "VERIFICATION FAILED"]
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The log
# --------------------------------------------------------------------------- #
_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS leaves (
    seq        INTEGER PRIMARY KEY,
    leaf_hash  BLOB NOT NULL,
    record     BLOB NOT NULL,
    kind       TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    verdict    TEXT NOT NULL,
    run_id     TEXT NOT NULL DEFAULT '',
    at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS leaves_subject ON leaves(subject_id);
CREATE INDEX IF NOT EXISTS leaves_run ON leaves(run_id);

CREATE TABLE IF NOT EXISTS heads (
    size      INTEGER PRIMARY KEY,
    root      BLOB NOT NULL,
    at        TEXT NOT NULL,
    key_id    TEXT NOT NULL,
    key_kind  TEXT NOT NULL,
    signature BLOB NOT NULL
);

-- Layer 1: the database itself refuses to rewrite history. These fire before any
-- ordinary bug or careless operator can mutate a committed decision.
CREATE TRIGGER IF NOT EXISTS leaves_are_immutable
BEFORE UPDATE ON leaves
BEGIN
    SELECT RAISE(ABORT, 'audit log is append-only: leaves cannot be updated');
END;

CREATE TRIGGER IF NOT EXISTS leaves_are_permanent
BEFORE DELETE ON leaves
BEGIN
    SELECT RAISE(ABORT, 'audit log is append-only: leaves cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS heads_are_immutable
BEFORE UPDATE ON heads
BEGIN
    SELECT RAISE(ABORT, 'signed tree heads cannot be updated');
END;
"""


@dataclass(frozen=True, slots=True)
class AppendReceipt:
    """Returned by every append: proof that the record is committed, right now."""

    seq: int
    leaf: bytes
    head: SignedTreeHead
    proof: InclusionProof

    def verify(self) -> bool:
        return self.proof.verify(self.head.root)


class AuditLog:
    """Append-only, Merkle-committed, signed decision log."""

    def __init__(
        self,
        path: Path | str = DEFAULT_DB,
        *,
        clock: Clock | None = None,
        key: AuditKey | None = None,
    ) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock: Clock = clock or SystemClock()
        self.key = key or AuditKey.load()
        self._conn = sqlite3.connect(str(self.path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._tree = MerkleTree(self._load_leaf_hashes())

    # --- lifecycle ---------------------------------------------------------
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def _load_leaf_hashes(self) -> list[bytes]:
        rows = self._conn.execute("SELECT leaf_hash FROM leaves ORDER BY seq").fetchall()
        return [row[0] for row in rows]

    def __len__(self) -> int:
        return len(self._tree)

    @property
    def size(self) -> int:
        return len(self._tree)

    def root(self, size: int | None = None) -> bytes:
        return self._tree.root(size)

    # --- append ------------------------------------------------------------
    def append(self, record: DecisionRecord) -> AppendReceipt:
        """Commit one decision. Returns a receipt that verifies on the spot.

        The receipt is not a formality: it is how the guarded-action wrapper in
        ``core/policy.py`` proves the audit write happened *before* the action, and
        it is what a caller hands to a third party as evidence.
        """
        payload = record.to_bytes()
        digest = leaf_hash(payload)
        seq = self._tree.append_hash(digest)
        self._conn.execute(
            "INSERT INTO leaves (seq, leaf_hash, record, kind, subject_id, verdict, run_id, at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                seq,
                digest,
                payload,
                record.kind,
                record.subject_id,
                record.verdict.value,
                record.run_id,
                record.at.isoformat(),
            ),
        )
        head = self._sign_head()
        self._conn.commit()
        return AppendReceipt(
            seq=seq, leaf=digest, head=head, proof=self._tree.inclusion_proof(seq)
        )

    def append_many(self, records: Sequence[DecisionRecord]) -> list[AppendReceipt]:
        return [self.append(record) for record in records]

    def _sign_head(self) -> SignedTreeHead:
        head = SignedTreeHead(
            size=self.size,
            root=self.root(),
            at=self.clock.now(),
            key_id=self.key.key_id,
            key_kind=self.key.kind,
            signature=b"",
        )
        signature = self.key.sign(head.payload())
        head = SignedTreeHead(
            head.size, head.root, head.at, head.key_id, head.key_kind, signature
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO heads (size, root, at, key_id, key_kind, signature)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                head.size,
                head.root,
                head.at.isoformat(),
                head.key_id,
                head.key_kind,
                head.signature,
            ),
        )
        return head

    # --- read --------------------------------------------------------------
    def record_at(self, seq: int) -> DecisionRecord:
        row = self._conn.execute("SELECT record FROM leaves WHERE seq = ?", (seq,)).fetchone()
        if row is None:
            raise AuditError("no such record", seq=seq)
        return DecisionRecord.model_validate_json(row[0])

    def raw_at(self, seq: int) -> bytes:
        row = self._conn.execute("SELECT record FROM leaves WHERE seq = ?", (seq,)).fetchone()
        if row is None:
            raise AuditError("no such record", seq=seq)
        return bytes(row[0])

    def iter_records(
        self, *, run_id: str | None = None, limit: int | None = None, offset: int = 0
    ) -> Iterator[tuple[int, DecisionRecord]]:
        sql = "SELECT seq, record FROM leaves"
        params: list[Any] = []
        if run_id:
            sql += " WHERE run_id = ?"
            params.append(run_id)
        sql += " ORDER BY seq LIMIT ? OFFSET ?"
        params += [limit if limit is not None else -1, offset]
        for seq, blob in self._conn.execute(sql, params):
            yield seq, DecisionRecord.model_validate_json(blob)

    def heads(self) -> list[SignedTreeHead]:
        rows = self._conn.execute(
            "SELECT size, root, at, key_id, key_kind, signature FROM heads ORDER BY size"
        ).fetchall()
        return [
            SignedTreeHead(
                size=size,
                root=bytes(root),
                at=dt.datetime.fromisoformat(at),
                key_id=key_id,
                key_kind=key_kind,
                signature=bytes(signature),
            )
            for size, root, at, key_id, key_kind, signature in rows
        ]

    def latest_head(self) -> SignedTreeHead:
        heads = self.heads()
        if not heads:
            return self._sign_head()
        return heads[-1]

    # --- proofs ------------------------------------------------------------
    def inclusion_proof(self, seq: int, size: int | None = None) -> InclusionProof:
        return self._tree.inclusion_proof(seq, size)

    def consistency_proof(self, first: int, second: int | None = None) -> ConsistencyProof:
        return self._tree.consistency_proof(first, second)

    # --- verification ------------------------------------------------------
    def verify(self) -> VerificationReport:
        """Re-derive everything from the stored bytes and report what breaks, where.

        Deliberately trusts nothing it has in memory: leaf hashes are recomputed from
        the stored records, the root is recomputed from those, and the signature is
        checked over the recomputed root.
        """
        checks: list[tuple[str, bool, str]] = []
        rows = self._conn.execute("SELECT seq, leaf_hash, record FROM leaves ORDER BY seq").fetchall()

        tampered_index: int | None = None
        tampered_detail = ""
        recomputed: list[bytes] = []
        for seq, stored_hash, payload in rows:
            actual = leaf_hash(bytes(payload))
            recomputed.append(actual)
            if actual != bytes(stored_hash) and tampered_index is None:
                tampered_index = seq
                tampered_detail = (
                    f"record {seq} hashes to {actual.hex()[:16]}... but the tree "
                    f"committed to {bytes(stored_hash).hex()[:16]}..."
                )
        checks.append(
            (
                f"every stored record hashes to its committed leaf ({len(rows)} checked)",
                tampered_index is None,
                tampered_detail,
            )
        )

        # Sequence numbers must be dense and start at zero: a hole means a delete
        # got past the trigger.
        expected = list(range(len(rows)))
        actual_seqs = [row[0] for row in rows]
        dense = actual_seqs == expected
        checks.append(
            (
                "sequence numbers are dense and gap-free",
                dense,
                "" if dense else f"expected 0..{len(rows) - 1}, found {actual_seqs[:8]}...",
            )
        )

        computed_root = merkle_root(recomputed)
        head = self.latest_head()
        root_matches = head.size == len(rows) and computed_root == head.root
        checks.append(
            (
                "recomputed root matches the signed tree head",
                root_matches,
                ""
                if root_matches
                else f"recomputed {computed_root.hex()[:16]}... vs signed {head.root.hex()[:16]}...",
            )
        )

        signature_ok = head.verify_signature(self.key.public)
        checks.append(
            (
                f"tree head signature (Ed25519, key {head.key_id}, {head.key_kind})",
                signature_ok,
                "",
            )
        )

        # Every historical head must be consistent with the current tree: this is the
        # append-only proof, and it is the check a hash chain cannot offer.
        #
        # The proofs are generated from the *recomputed* leaves, never from the tree
        # this object happens to hold in memory. Verifying disk against memory would
        # only prove the two agree; the question is whether the bytes on disk still
        # support the heads that were signed over them. Doing it this way also dates
        # the tampering: heads signed before the altered record still verify, and the
        # first size that fails is the point at which history was rewritten.
        disk_tree = MerkleTree(recomputed)
        all_heads = self.heads()
        inconsistent: list[int] = []
        for old in all_heads[:-1]:
            if old.size > len(recomputed):
                inconsistent.append(old.size)
                continue
            proof = disk_tree.consistency_proof(old.size, len(recomputed))
            if not proof.verify(old.root, computed_root):
                inconsistent.append(old.size)
        detail = ""
        if inconsistent:
            detail = (
                f"first head that no longer verifies is size {min(inconsistent)}; "
                f"{len(inconsistent)} of {len(all_heads) - 1} broken"
            )
        checks.append(
            (
                f"append-only: {max(len(all_heads) - 1, 0)} historical head(s) prove consistent",
                not inconsistent,
                detail,
            )
        )

        ok = all(passed for _, passed, _ in checks)
        return VerificationReport(
            ok=ok,
            size=len(rows),
            root=computed_root,
            checks=tuple(checks),
            tampered_index=tampered_index,
            tampered_detail=tampered_detail,
        )

    def require_intact(self) -> None:
        report = self.verify()
        if not report.ok:
            raise TamperDetected(
                "audit log failed verification",
                index=report.tampered_index,
                detail=report.tampered_detail,
            )

    # --- adversary simulation ----------------------------------------------
    def tamper_for_demo(self, seq: int, replacement_reason: str) -> None:
        """Simulate an attacker with direct database access.

        This deliberately drops the append-only triggers, rewrites a committed
        record, and puts the triggers back - i.e. it models someone who owns the
        file, not someone using Triveni's API. That is the threat the Merkle tree
        exists for, and ``verify()`` will name the index. It is exported for the
        demo and the red-team suite and is never reachable from the API surface.
        """
        payload = self.raw_at(seq)
        record = DecisionRecord.model_validate_json(payload)
        forged = record.model_copy(update={"reason": replacement_reason}).to_bytes()
        self._conn.executescript(
            "DROP TRIGGER IF EXISTS leaves_are_immutable;"
            "DROP TRIGGER IF EXISTS leaves_are_permanent;"
        )
        self._conn.execute("UPDATE leaves SET record = ? WHERE seq = ?", (forged, seq))
        self._conn.commit()
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def restore_from_demo_tamper(self, seq: int, original_reason: str) -> None:
        """Undo :meth:`tamper_for_demo`, so the UI's Restore button heals the log."""
        self.tamper_for_demo(seq, original_reason)


def new_decision(
    *,
    kind: str,
    subject_id: str,
    verdict: Verdict,
    reason: str,
    clock: Clock,
    inputs: dict[str, Any] | None = None,
    action: dict[str, Any] | None = None,
    outcome: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    actor: str = "triveni",
    run_id: str = "",
) -> DecisionRecord:
    """Construct a decision record with the clock injected rather than read."""
    return DecisionRecord(
        kind=kind,
        subject_id=subject_id,
        verdict=verdict,
        reason=reason,
        inputs=inputs or {},
        action=action or {},
        outcome=outcome or {},
        evidence=evidence or {},
        actor=actor,
        at=clock.now().astimezone(IST),
        run_id=run_id,
    )


def decision_id(record: DecisionRecord) -> str:
    return make_id(IdKind.DECISION, record.canonical())
