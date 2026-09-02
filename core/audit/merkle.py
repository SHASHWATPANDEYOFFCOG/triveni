"""An RFC 6962 / RFC 9162 Merkle transparency log, over financial decisions.

Most audit trails are a SHA-256 hash chain: each record hashes the one before it. A
chain proves nothing useful on its own. To check that record 4,000 is in it you must
replay all 4,000, and it cannot prove the log is *append-only* - a log operator can
rewrite history and re-chain, and every individual link still verifies.

Certificate Transparency solved this for the web PKI, and the construction transfers
directly. A Merkle tree gives two proofs a hash chain cannot:

* **inclusion** - "decision 4,000 is committed under this root", in O(log n) hashes,
  without trusting or replaying the rest of the log;
* **consistency** - "the tree of size 8,192 contains, unmodified, everything the tree
  of size 4,000 contained". This is the append-only property, proved rather than
  promised. It is what stops a merchant, or Triveni itself, from quietly rewriting
  yesterday's reconciliation after the fact.

Hashing follows RFC 6962 exactly, prefix bytes and all, because the value of using
the standard construction is that a judge can check it against the RFC rather than
against our prose:

    MTH({})        = SHA-256()                       (the empty string's hash)
    MTH({d})       = SHA-256(0x00 || d)              (leaf)
    MTH(D[n])      = SHA-256(0x01 || MTH(D[0:k]) || MTH(D[k:n]))
                     where k is the largest power of two strictly less than n

The 0x00/0x01 domain separation is what stops an attacker presenting an internal node
as though it were a leaf (second-preimage), which is precisely the attack a naive
"hash the concatenation" tree is open to.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from core.errors import AuditError

#: RFC 6962 domain separation prefixes.
LEAF_PREFIX: Final = b"\x00"
NODE_PREFIX: Final = b"\x01"

#: MTH of the empty tree is the hash of the empty string.
EMPTY_ROOT: Final = hashlib.sha256(b"").digest()

HASH_LEN: Final = 32


def leaf_hash(data: bytes) -> bytes:
    """MTH({data}) - the hash of a single leaf."""
    return hashlib.sha256(LEAF_PREFIX + data).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    """The hash of an internal node with the given children."""
    return hashlib.sha256(NODE_PREFIX + left + right).digest()


def _split_point(n: int) -> int:
    """The largest power of two strictly less than ``n`` (RFC 6962's ``k``)."""
    if n < 2:
        raise AuditError("split point is undefined below size 2", size=n)
    return 1 << ((n - 1).bit_length() - 1)


def merkle_root(leaves: Sequence[bytes]) -> bytes:
    """MTH(D[n]) computed iteratively.

    The recursive definition is the readable one, but a day's ledger can be tens of
    thousands of decisions and Python's stack is not. This walks levels bottom-up,
    carrying an odd node up unchanged - which is exactly what the recursive split
    produces, because ``k`` is a power of two, so the left subtree is always perfect
    and only the right one can be ragged.
    """
    if not leaves:
        return EMPTY_ROOT
    level = list(leaves)
    while len(level) > 1:
        nxt: list[bytes] = []
        for i in range(0, len(level) - 1, 2):
            nxt.append(node_hash(level[i], level[i + 1]))
        if len(level) % 2:
            nxt.append(level[-1])
        level = nxt
    return level[0]


def merkle_root_recursive(leaves: Sequence[bytes]) -> bytes:
    """The literal RFC definition. Kept as the oracle the fast path is tested against
    - if these two ever disagree, the fast path is wrong."""
    n = len(leaves)
    if n == 0:
        return EMPTY_ROOT
    if n == 1:
        return leaves[0]
    k = _split_point(n)
    return node_hash(merkle_root_recursive(leaves[:k]), merkle_root_recursive(leaves[k:]))


# --------------------------------------------------------------------------- #
# Proof generation (RFC 6962 section 2.1.1 / 2.1.2)
# --------------------------------------------------------------------------- #
def inclusion_path(leaves: Sequence[bytes], index: int) -> list[bytes]:
    """PATH(m, D[n]) - the sibling hashes needed to recompute the root from leaf m."""
    n = len(leaves)
    if not 0 <= index < n:
        raise AuditError("leaf index out of range", index=index, size=n)
    if n == 1:
        return []
    k = _split_point(n)
    if index < k:
        return [*inclusion_path(leaves[:k], index), merkle_root(leaves[k:])]
    return [*inclusion_path(leaves[k:], index - k), merkle_root(leaves[:k])]


def consistency_path(leaves: Sequence[bytes], first: int) -> list[bytes]:
    """PROOF(m, D[n]) - proof that the size-``first`` tree is a prefix of this one."""
    n = len(leaves)
    if not 0 <= first <= n:
        raise AuditError("consistency size out of range", first=first, size=n)
    if first == 0:
        # Every tree extends the empty tree; nothing to prove.
        return []
    return _subproof(leaves, first, True)


def _subproof(leaves: Sequence[bytes], m: int, is_root: bool) -> list[bytes]:
    n = len(leaves)
    if m == n:
        return [] if is_root else [merkle_root(leaves)]
    k = _split_point(n)
    if m <= k:
        return [*_subproof(leaves[:k], m, is_root), merkle_root(leaves[k:])]
    return [*_subproof(leaves[k:], m - k, False), merkle_root(leaves[:k])]


# --------------------------------------------------------------------------- #
# Proof verification (RFC 9162 section 2.1.3.2 / 2.1.4.2)
# --------------------------------------------------------------------------- #
def verify_inclusion(
    *, leaf: bytes, index: int, size: int, path: Sequence[bytes], root: bytes
) -> bool:
    """Verify that ``leaf`` sits at ``index`` in the size-``size`` tree with ``root``.

    Written as the RFC writes it, deliberately: a verifier is exactly the code you
    want a reader to be able to diff against the specification.
    """
    if index >= size or size == 0:
        return False
    fn, sn = index, size - 1
    r = leaf
    for p in path:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            r = node_hash(p, r)
            while fn != 0 and not fn & 1:
                fn >>= 1
                sn >>= 1
        else:
            r = node_hash(r, p)
        fn >>= 1
        sn >>= 1
    return sn == 0 and r == root


def verify_consistency(
    *, first_size: int, second_size: int, path: Sequence[bytes], first_root: bytes, second_root: bytes
) -> bool:
    """Verify that the size-``first_size`` tree is a prefix of the size-``second_size``
    tree - i.e. that nothing already logged was altered or removed.

    This is the proof that matters for an audit log. Inclusion says a record is
    present; consistency says the past was not rewritten to make it so.
    """
    if first_size > second_size:
        return False
    if first_size == second_size:
        return not path and first_root == second_root
    if first_size == 0:
        return not path
    proof = list(path)
    if first_size & (first_size - 1) == 0:
        # first_size is a power of two, so the old root is itself a node of the new
        # tree and is not transmitted; the verifier supplies it.
        proof.insert(0, first_root)
    if not proof:
        return False

    fn, sn = first_size - 1, second_size - 1
    while fn & 1:
        fn >>= 1
        sn >>= 1

    fr = sr = proof[0]
    for c in proof[1:]:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            fr = node_hash(c, fr)
            sr = node_hash(c, sr)
            while fn != 0 and not fn & 1:
                fn >>= 1
                sn >>= 1
        else:
            sr = node_hash(sr, c)
        fn >>= 1
        sn >>= 1

    return sn == 0 and fr == first_root and sr == second_root


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class InclusionProof:
    """A self-contained answer to 'is this decision in the log?'.

    Carries everything a third party needs; they do not need the log, or Triveni,
    or our good faith - just this object and the published root.
    """

    leaf: bytes
    index: int
    tree_size: int
    path: tuple[bytes, ...]

    def verify(self, root: bytes) -> bool:
        return verify_inclusion(
            leaf=self.leaf, index=self.index, size=self.tree_size, path=self.path, root=root
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "leaf": self.leaf.hex(),
            "index": self.index,
            "tree_size": self.tree_size,
            "path": [h.hex() for h in self.path],
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> InclusionProof:
        """Rebuild a proof received from elsewhere - the point of the wire format is
        that a third party can verify without running any of our code."""
        return cls(
            leaf=bytes.fromhex(payload["leaf"]),
            index=int(payload["index"]),
            tree_size=int(payload["tree_size"]),
            path=tuple(bytes.fromhex(h) for h in payload["path"]),
        )


@dataclass(frozen=True, slots=True)
class ConsistencyProof:
    """A self-contained answer to 'was the log appended to, or rewritten?'."""

    first_size: int
    second_size: int
    path: tuple[bytes, ...]

    def verify(self, first_root: bytes, second_root: bytes) -> bool:
        return verify_consistency(
            first_size=self.first_size,
            second_size=self.second_size,
            path=self.path,
            first_root=first_root,
            second_root=second_root,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "first_size": self.first_size,
            "second_size": self.second_size,
            "path": [h.hex() for h in self.path],
        }


@dataclass(slots=True)
class MerkleTree:
    """An in-memory tree over leaf hashes, with proof generation.

    The audit log owns the durable copy; this is the computational core, kept
    separate so it can be tested exhaustively without touching a database.
    """

    leaves: list[bytes]

    def __init__(self, leaves: Sequence[bytes] | None = None) -> None:
        self.leaves = list(leaves or [])

    def __len__(self) -> int:
        return len(self.leaves)

    def append(self, data: bytes) -> int:
        """Append raw leaf *data* (it is hashed here) and return its index."""
        self.leaves.append(leaf_hash(data))
        return len(self.leaves) - 1

    def append_hash(self, digest: bytes) -> int:
        if len(digest) != HASH_LEN:
            raise AuditError("leaf hash must be 32 bytes", length=len(digest))
        self.leaves.append(digest)
        return len(self.leaves) - 1

    def root(self, size: int | None = None) -> bytes:
        """Root of the whole tree, or of its first ``size`` leaves."""
        if size is None:
            return merkle_root(self.leaves)
        if not 0 <= size <= len(self.leaves):
            raise AuditError("root requested for a size the log has not reached", size=size)
        return merkle_root(self.leaves[:size])

    def inclusion_proof(self, index: int, size: int | None = None) -> InclusionProof:
        size = len(self.leaves) if size is None else size
        if not 0 <= size <= len(self.leaves):
            raise AuditError("proof requested beyond the log", size=size)
        if not 0 <= index < size:
            # Validated before indexing so callers get a typed error carrying the
            # context, never a bare IndexError from a list access.
            raise AuditError("leaf index out of range", index=index, size=size)
        return InclusionProof(
            leaf=self.leaves[index],
            index=index,
            tree_size=size,
            path=tuple(inclusion_path(self.leaves[:size], index)),
        )

    def consistency_proof(self, first: int, second: int | None = None) -> ConsistencyProof:
        second = len(self.leaves) if second is None else second
        if not 0 <= first <= second <= len(self.leaves):
            raise AuditError("inconsistent sizes", first=first, second=second)
        return ConsistencyProof(
            first_size=first,
            second_size=second,
            path=tuple(consistency_path(self.leaves[:second], first)),
        )
