"""M2 gate: the audit log is tamper-evident, append-only and provable.

The DoD is specific: mutate row *n* and the verifier must report index *n*, and a
consistency proof between two roots must pass. Both are here, plus exhaustive
verification of the Merkle construction itself against the literal RFC 6962
definition - because an audit log that is subtly wrong is worse than none, and
"it worked on the one case I tried" is exactly the failure this milestone exists
to rule out.
"""

from __future__ import annotations

import hashlib
import sqlite3

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from core.audit.log import (
    AuditKey,
    AuditLog,
    DecisionRecord,
    Verdict,
    new_decision,
)
from core.audit.merkle import (
    EMPTY_ROOT,
    ConsistencyProof,
    InclusionProof,
    MerkleTree,
    leaf_hash,
    merkle_root,
    merkle_root_recursive,
    node_hash,
)
from core.clock import FrozenClock
from core.errors import AuditError, TamperDetected

CLOCK = FrozenClock.at("2026-03-31 18:30")


def make_tree(n: int) -> MerkleTree:
    tree = MerkleTree()
    for i in range(n):
        tree.append(f"decision-{i}".encode())
    return tree


def make_log(tmp_path, n: int = 12, *, name: str = "audit.db") -> AuditLog:
    log = AuditLog(tmp_path / name, clock=FrozenClock.at("2026-03-31 18:30"))
    for i in range(n):
        log.append(
            new_decision(
                kind="match.accept",
                subject_id=f"mtc_{i:04d}",
                verdict=Verdict.AUTO_POSTED,
                reason=f"exact UTR match on row {i}",
                clock=CLOCK,
                inputs={"utr": f"UTR{i:08d}"},
                outcome={"posted": True},
                run_id="run_test",
            )
        )
    return log


# --------------------------------------------------------------------------- #
# The construction, against the RFC
# --------------------------------------------------------------------------- #
def test_empty_tree_root_is_the_hash_of_the_empty_string() -> None:
    """RFC 6962: MTH({}) = SHA-256()."""
    assert merkle_root([]) == hashlib.sha256(b"").digest() == EMPTY_ROOT


def test_leaf_and_node_hashing_use_the_rfc_domain_separation() -> None:
    """The 0x00/0x01 prefixes are what stop an internal node being presented as a
    leaf. Without them the tree is open to a second-preimage attack."""
    assert leaf_hash(b"x") == hashlib.sha256(b"\x00x").digest()
    assert node_hash(b"l" * 32, b"r" * 32) == hashlib.sha256(b"\x01" + b"l" * 32 + b"r" * 32).digest()
    assert leaf_hash(b"a" * 64) != node_hash(b"a" * 32, b"a" * 32)


@pytest.mark.parametrize("n", list(range(0, 130)))
def test_iterative_root_equals_the_literal_recursive_definition(n: int) -> None:
    """The fast path is only allowed to exist if it agrees with the RFC's own
    recursive formulation at every size, including the ragged ones."""
    leaves = [leaf_hash(bytes([i % 251])) for i in range(n)]
    assert merkle_root(leaves) == merkle_root_recursive(leaves)


def test_root_is_order_sensitive() -> None:
    """Reordering the log must change the root - otherwise 'append-only' is empty."""
    a = MerkleTree([leaf_hash(b"one"), leaf_hash(b"two")])
    b = MerkleTree([leaf_hash(b"two"), leaf_hash(b"one")])
    assert a.root() != b.root()


# --------------------------------------------------------------------------- #
# Inclusion proofs
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("n", list(range(1, 65)))
def test_every_leaf_of_every_tree_size_proves_inclusion(n: int) -> None:
    tree = make_tree(n)
    root = tree.root()
    for index in range(n):
        assert tree.inclusion_proof(index).verify(root), f"n={n} index={index}"


def test_inclusion_proof_is_logarithmic() -> None:
    """The property a hash chain cannot offer: verify one record out of 100,000
    without replaying the other 99,999."""
    tree = MerkleTree([leaf_hash(bytes([i % 251])) for i in range(100_000)])
    proof = tree.inclusion_proof(54_321)
    assert len(proof.path) == 17 == (100_000 - 1).bit_length()
    assert proof.verify(tree.root())


def test_inclusion_proof_rejects_every_shape_of_forgery() -> None:
    tree = make_tree(17)
    root = tree.root()
    good = tree.inclusion_proof(9)

    assert good.verify(root)
    assert not good.verify(leaf_hash(b"some other root"))
    assert not InclusionProof(good.leaf, 8, good.tree_size, good.path).verify(root)
    assert not InclusionProof(leaf_hash(b"forged"), 9, good.tree_size, good.path).verify(root)
    assert not InclusionProof(good.leaf, 9, good.tree_size, good.path[:-1]).verify(root)
    assert not InclusionProof(good.leaf, 9, good.tree_size, (bytes(32), *good.path[1:])).verify(root)
    assert not InclusionProof(good.leaf, 9, good.tree_size, (*good.path, bytes(32))).verify(root)


def test_out_of_range_index_is_a_typed_error() -> None:
    with pytest.raises(AuditError):
        make_tree(4).inclusion_proof(9)


def test_inclusion_proof_serialises_round_trip() -> None:
    tree = make_tree(11)
    proof = tree.inclusion_proof(6)
    assert InclusionProof.from_json(proof.to_json()) == proof
    assert InclusionProof.from_json(proof.to_json()).verify(tree.root())


# --------------------------------------------------------------------------- #
# Consistency proofs - the append-only property
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("n", list(range(0, 40)))
def test_every_prefix_of_every_tree_proves_consistency(n: int) -> None:
    tree = make_tree(n)
    for first in range(n + 1):
        proof = tree.consistency_proof(first, n)
        assert proof.verify(tree.root(first), tree.root(n)), f"n={n} first={first}"


def test_consistency_proof_rejects_a_rewritten_history() -> None:
    """The whole point. An operator who edits an old record and then appends more
    cannot produce a proof that the new tree extends the old one."""
    honest = make_tree(17)
    old_root = honest.root()

    rewritten = MerkleTree(list(honest.leaves))
    rewritten.leaves[3] = leaf_hash(b"quietly changed after the fact")
    for i in range(17, 25):
        rewritten.append(f"decision-{i}".encode())

    proof = rewritten.consistency_proof(17, 25)
    assert not proof.verify(old_root, rewritten.root())


def test_consistency_proof_accepts_an_honest_append() -> None:
    honest = make_tree(17)
    old_root = honest.root()
    extended = MerkleTree(list(honest.leaves))
    for i in range(17, 25):
        extended.append(f"decision-{i}".encode())
    assert extended.consistency_proof(17, 25).verify(old_root, extended.root())


def test_consistency_to_the_same_size_needs_no_proof() -> None:
    tree = make_tree(9)
    assert tree.consistency_proof(9, 9).verify(tree.root(9), tree.root(9))
    assert not ConsistencyProof(9, 9, (bytes(32),)).verify(tree.root(9), tree.root(9))


def test_shrinking_the_log_is_never_consistent() -> None:
    tree = make_tree(9)
    assert not ConsistencyProof(9, 4, ()).verify(tree.root(9), tree.root(4))


@given(st.integers(min_value=1, max_value=48), st.data())
@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_truncated_consistency_proofs_never_verify(n: int, data: st.DataObject) -> None:
    tree = make_tree(n)
    first = data.draw(st.integers(min_value=1, max_value=n))
    proof = tree.consistency_proof(first, n)
    if proof.path:
        broken = ConsistencyProof(first, n, proof.path[:-1])
        assert not broken.verify(tree.root(first), tree.root(n))


# --------------------------------------------------------------------------- #
# The durable log
# --------------------------------------------------------------------------- #
def test_every_append_returns_a_receipt_that_verifies(tmp_path) -> None:
    log = make_log(tmp_path)
    for seq in range(len(log)):
        assert log.inclusion_proof(seq).verify(log.root())
    assert log.verify().ok


def test_a_reason_is_mandatory_by_construction() -> None:
    """Non-negotiable 1, at the type level: you cannot write to this log without
    saying why."""
    with pytest.raises(ValueError):
        DecisionRecord(
            kind="match.accept",
            subject_id="mtc_1",
            verdict=Verdict.AUTO_POSTED,
            reason="",
            at=CLOCK.now(),
        )


def test_sqlite_triggers_refuse_update_and_delete(tmp_path) -> None:
    """Layer 1: ordinary bugs and careless operators cannot rewrite a decision."""
    log = make_log(tmp_path, 5)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        log._conn.execute("UPDATE leaves SET record = X'00' WHERE seq = 2")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        log._conn.execute("DELETE FROM leaves WHERE seq = 2")
    with pytest.raises(sqlite3.IntegrityError):
        log._conn.execute("UPDATE heads SET root = X'00' WHERE size = 2")


@pytest.mark.parametrize("victim", [0, 1, 4, 7, 11])
def test_tampering_with_row_n_is_reported_at_index_n(tmp_path, victim: int) -> None:
    """The M2 Definition of Done, at every position in the log."""
    log = make_log(tmp_path, 12, name=f"audit-{victim}.db")
    assert log.verify().ok

    log.tamper_for_demo(victim, "approved by finance head")

    report = log.verify()
    assert not report.ok
    assert report.tampered_index == victim
    assert f"record {victim}" in report.tampered_detail


def test_tampering_dates_itself_as_well_as_locating_itself(tmp_path) -> None:
    """Heads signed before the altered record still verify; the first size that
    breaks is the point at which history was rewritten. Index *and* time."""
    log = make_log(tmp_path, 12)
    log.tamper_for_demo(4, "approved by finance head")
    report = log.verify()

    named = {name: (passed, detail) for name, passed, detail in report.checks}
    append_only = next(v for k, v in named.items() if k.startswith("append-only"))
    assert append_only[0] is False
    assert "size 5" in append_only[1], append_only[1]


def test_restoring_the_record_heals_the_log(tmp_path) -> None:
    """The UI's Restore button, tested."""
    log = make_log(tmp_path, 12)
    original = log.record_at(6).reason
    log.tamper_for_demo(6, "approved by finance head")
    assert not log.verify().ok
    log.restore_from_demo_tamper(6, original)
    assert log.verify().ok


def test_require_intact_raises_a_typed_error(tmp_path) -> None:
    log = make_log(tmp_path, 6)
    log.require_intact()
    log.tamper_for_demo(2, "forged")
    with pytest.raises(TamperDetected) as excinfo:
        log.require_intact()
    assert excinfo.value.context["index"] == 2


# --------------------------------------------------------------------------- #
# Signing
# --------------------------------------------------------------------------- #
def test_tree_head_is_signed_and_verifies(tmp_path) -> None:
    log = make_log(tmp_path, 8)
    head = log.latest_head()
    assert head.size == 8
    assert head.verify_signature(log.key.public)
    assert len(head.signature) == 64


def test_a_forged_tree_head_does_not_verify(tmp_path) -> None:
    """An attacker who rewrites the log *and* recomputes the root still cannot sign."""
    from core.audit.log import SignedTreeHead

    log = make_log(tmp_path, 8)
    head = log.latest_head()
    forged = SignedTreeHead(
        head.size, leaf_hash(b"a root of my choosing"), head.at, head.key_id, head.key_kind, head.signature
    )
    assert not forged.verify_signature(log.key.public)


def test_demo_key_is_deterministic_and_labelled_as_such(monkeypatch) -> None:
    """Reproducible for a judge, and stamped so it cannot be mistaken for a
    custodied production key."""
    monkeypatch.setenv("TRIVENI_SEED", "20260101")
    monkeypatch.delenv("TRIVENI_AUDIT_KEY", raising=False)
    first, second = AuditKey.load(), AuditKey.load()
    assert first.public_bytes == second.public_bytes
    assert first.kind == "demo-deterministic"

    monkeypatch.setenv("TRIVENI_SEED", "different")
    assert AuditKey.load().public_bytes != first.public_bytes


def test_operator_supplied_key_takes_precedence(monkeypatch) -> None:
    monkeypatch.setenv("TRIVENI_AUDIT_KEY", "11" * 32)
    key = AuditKey.load()
    assert key.kind == "operator-supplied"
    monkeypatch.setenv("TRIVENI_AUDIT_KEY", "not-hex")
    with pytest.raises(AuditError):
        AuditKey.load()


def test_the_whole_log_is_reproducible_from_the_seed(tmp_path, monkeypatch) -> None:
    """Same seed, same records, same root, same signature - on any machine. This is
    what lets a judge re-run `make demo` and compare the published root hash."""
    monkeypatch.setenv("TRIVENI_SEED", "20260101")
    a = make_log(tmp_path, 12, name="a.db")
    b = make_log(tmp_path, 12, name="b.db")
    assert a.root() == b.root()
    assert a.latest_head().signature == b.latest_head().signature


# --------------------------------------------------------------------------- #
# The CLI
# --------------------------------------------------------------------------- #
def test_verify_cli_exits_zero_on_an_intact_log(tmp_path, capsys) -> None:
    from core.audit import verify as verify_cli

    log = make_log(tmp_path, 10)
    log.close()
    assert verify_cli.main(["--db", str(tmp_path / "audit.db")]) == 0
    assert "VERIFIED" in capsys.readouterr().out


def test_verify_cli_exits_nonzero_and_names_the_index_on_a_tampered_log(tmp_path, capsys) -> None:
    from core.audit import verify as verify_cli

    log = make_log(tmp_path, 10)
    log.close()
    db = str(tmp_path / "audit.db")
    assert verify_cli.main(["--db", db, "--tamper", "3"]) == 1
    out = capsys.readouterr().out
    assert "TAMPERING LOCALISED AT INDEX 3" in out


def test_verify_cli_emits_machine_readable_proofs(tmp_path, capsys) -> None:
    import json

    from core.audit import verify as verify_cli

    log = make_log(tmp_path, 10)
    log.close()
    db = str(tmp_path / "audit.db")
    assert verify_cli.main(["--db", db, "--prove", "4", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verified"] is True
    assert len(payload["proof"]["path"]) >= 1

    assert verify_cli.main(["--db", db, "--consistency", "3", "10", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verified"] is True


def test_verify_cli_is_helpful_when_there_is_no_log(tmp_path, capsys) -> None:
    from core.audit import verify as verify_cli

    assert verify_cli.main(["--db", str(tmp_path / "missing.db")]) == 2
    assert "make demo" in capsys.readouterr().out
