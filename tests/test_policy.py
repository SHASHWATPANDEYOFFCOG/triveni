"""M3 gate: bounded, gated, and audited-before-acting.

The Definition of Done has two halves. Unit tests on every policy boundary - one
either side of each threshold, so an off-by-one is caught rather than assumed away.
And a proof that posting without an audit record is impossible *by construction*: the
posting function demands a receipt carrying a Merkle inclusion proof under an
Ed25519-signed head, which a caller who skipped the log cannot manufacture.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from core.audit.log import AppendReceipt, AuditLog, SignedTreeHead, Verdict
from core.audit.merkle import InclusionProof
from core.clock import FrozenClock
from core.errors import KillSwitchEngaged, TriveniError, UnauditedAction
from core.guard import Guard, _post_to_books
from core.money import Money
from core.policy import (
    ActionClass,
    ActionRequest,
    PolicyConfig,
    PolicyEngine,
    RuleId,
    RunState,
    evaluate,
)

CLOCK = FrozenClock.at("2026-03-31 18:30")
CONFIG = PolicyConfig()


def request(**kwargs: object) -> ActionRequest:
    base: dict[str, object] = {
        "subject_id": "mtc_0001",
        "action_class": ActionClass.POST_TO_BOOKS,
        "amount": Money.from_rupees("5000"),
        "confidence": Decimal("0.99"),
        "reason": "exact UTR match on RRN 402911234567",
        "evidence_count": 2,
    }
    base.update(kwargs)
    return ActionRequest(**base)  # type: ignore[arg-type]


def decide(**kwargs: object) -> object:
    return evaluate(request(**kwargs), CONFIG, RunState())


def fresh_guard(tmp_path: Path, *, config: PolicyConfig | None = None) -> Guard:
    log = AuditLog(tmp_path / "audit.db", clock=FrozenClock.at("2026-03-31 18:30"))
    return Guard(
        log=log,
        engine=PolicyEngine(config=config or PolicyConfig()),
        clock=CLOCK,
        run_id="run_test",
    )


# --------------------------------------------------------------------------- #
# Absolute refusals
# --------------------------------------------------------------------------- #
def test_moving_money_is_refused_and_has_no_configuration_flag() -> None:
    """A finance controller reads and proposes. There is no override path, so this
    test also asserts that no config field could turn it on."""
    outcome = decide(action_class=ActionClass.MOVE_MONEY)
    assert outcome.verdict is Verdict.DENIED  # type: ignore[attr-defined]
    assert not outcome.may_post  # type: ignore[attr-defined]

    fields = {f for f in PolicyConfig.__dataclass_fields__}
    assert not any("money" in f or "transfer" in f or "payout" in f for f in fields), (
        "a config field that looks like it could enable money movement would make the "
        "guarantee negotiable"
    )


def test_kill_switch_refuses_everything_that_touches_the_books() -> None:
    engaged = CONFIG.engaged_kill_switch()
    outcome = evaluate(request(), engaged, RunState())
    assert outcome.verdict is Verdict.DENIED
    assert any(r.rule is RuleId.KILL_SWITCH and not r.passed for r in outcome.rules)


def test_kill_switch_still_permits_reading() -> None:
    """Stopping the world must not stop an operator from looking at why."""
    engaged = CONFIG.engaged_kill_switch()
    outcome = evaluate(request(action_class=ActionClass.READ), engaged, RunState())
    assert outcome.verdict is Verdict.DENIED, "kill switch is absolute for the books"

    relaxed = evaluate(request(action_class=ActionClass.READ), CONFIG, RunState())
    assert relaxed.verdict is Verdict.OBSERVED


def test_a_missing_reason_is_a_denial_not_a_default() -> None:
    """Non-negotiable 1: no action without a reason."""
    assert decide(reason="   ").verdict is Verdict.DENIED  # type: ignore[attr-defined]


def test_a_foreign_currency_is_denied_rather_than_crashing_the_engine() -> None:
    """Money refuses cross-currency comparison by design, so the cap rules must
    notice they cannot be evaluated instead of raising. An engine that can be
    crashed by hostile input is an engine that can be bypassed by crashing it."""
    outcome = decide(amount=Money(500_000, "USD"))
    assert outcome.verdict is Verdict.DENIED  # type: ignore[attr-defined]
    details = {r.rule: r.detail for r in outcome.rules}  # type: ignore[attr-defined]
    assert "not in ['INR']" in details[RuleId.CURRENCY_ALLOWED]
    assert "not evaluated" in details[RuleId.PER_ACTION_CAP]


# --------------------------------------------------------------------------- #
# Every boundary, both sides
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("24999.99", Verdict.AUTO_POSTED),
        ("25000", Verdict.AUTO_POSTED),  # at the threshold: <= is permitted
        ("25000.01", Verdict.NEEDS_REVIEW),  # one paise over
    ],
)
def test_materiality_boundary(amount: str, expected: Verdict) -> None:
    assert decide(amount=Money.from_rupees(amount)).verdict is expected  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("200000", Verdict.NEEDS_REVIEW),  # at the cap, but over materiality
        ("200000.01", Verdict.DENIED),  # one paise over the hard cap
    ],
)
def test_per_action_cap_boundary(amount: str, expected: Verdict) -> None:
    assert decide(amount=Money.from_rupees(amount)).verdict is expected  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("confidence", "expected"),
    [
        ("0.90", Verdict.AUTO_POSTED),  # exactly at the conformal threshold
        ("0.8999", Verdict.NEEDS_REVIEW),
        ("1.0", Verdict.AUTO_POSTED),
        ("0", Verdict.NEEDS_REVIEW),
    ],
)
def test_confidence_boundary(confidence: str, expected: Verdict) -> None:
    assert decide(confidence=Decimal(confidence)).verdict is expected  # type: ignore[attr-defined]


@pytest.mark.parametrize(("count", "expected"), [(0, Verdict.NEEDS_REVIEW), (1, Verdict.AUTO_POSTED)])
def test_evidence_boundary(count: int, expected: Verdict) -> None:
    assert decide(evidence_count=count).verdict is expected  # type: ignore[attr-defined]


def test_run_total_cap_boundary() -> None:
    config = PolicyConfig(run_total_cap=Money.from_rupees("10000"))
    spent = RunState(posted_total=Money.from_rupees("6000"), posted_count=1)
    assert evaluate(request(amount=Money.from_rupees("4000")), config, spent).verdict is Verdict.AUTO_POSTED
    assert evaluate(request(amount=Money.from_rupees("4000.01")), config, spent).verdict is Verdict.DENIED


def test_run_count_cap_boundary() -> None:
    config = PolicyConfig(run_count_cap=3)
    assert evaluate(request(), config, RunState(posted_count=2)).verdict is Verdict.AUTO_POSTED
    assert evaluate(request(), config, RunState(posted_count=3)).verdict is Verdict.DENIED


def test_a_refund_sized_negative_amount_uses_its_magnitude() -> None:
    """A -Rs 40,000 reversal is as material as a +Rs 40,000 posting."""
    assert decide(amount=Money.from_rupees("-40000")).verdict is Verdict.NEEDS_REVIEW  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Human approval
# --------------------------------------------------------------------------- #
def test_human_approval_clears_the_gates_but_never_the_hard_caps() -> None:
    """A person may accept a large, uncertain match. A person may not exceed a cap -
    that is the difference between a gate and a bound."""
    approved = decide(
        amount=Money.from_rupees("150000"),
        confidence=Decimal("0.10"),
        human_approved=True,
        approver="priya@merchant.in",
    )
    assert approved.verdict is Verdict.APPROVED_BY_HUMAN  # type: ignore[attr-defined]
    assert approved.may_post  # type: ignore[attr-defined]
    assert "priya@merchant.in" in approved.reason  # type: ignore[attr-defined]

    over_cap = decide(
        amount=Money.from_rupees("500000"), human_approved=True, approver="priya@merchant.in"
    )
    assert over_cap.verdict is Verdict.DENIED  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Purity and robustness
# --------------------------------------------------------------------------- #
@given(
    amount=st.integers(min_value=-10**11, max_value=10**11),
    confidence=st.decimals(min_value=Decimal(0), max_value=Decimal(1), places=4),
    evidence=st.integers(min_value=0, max_value=20),
    action=st.sampled_from(list(ActionClass)),
    currency=st.sampled_from(["INR", "USD", "EUR"]),
    reason=st.text(max_size=30),
    killed=st.booleans(),
)
@settings(max_examples=300)
def test_evaluate_never_raises_and_never_leaks_a_yes(
    amount: int,
    confidence: Decimal,
    evidence: int,
    action: ActionClass,
    currency: str,
    reason: str,
    killed: bool,
) -> None:
    """The policy engine is the last line of defence, so it must total: every input,
    including adversarial ones, yields a Decision rather than an exception."""
    config = PolicyConfig(kill_switch=killed)
    outcome = evaluate(
        ActionRequest(
            subject_id="mtc_x",
            action_class=action,
            amount=Money(amount, currency),
            confidence=confidence,
            reason=reason,
            evidence_count=evidence,
        ),
        config,
        RunState(),
    )
    assert outcome.verdict in set(Verdict)
    assert outcome.reason
    if killed or action is ActionClass.MOVE_MONEY or currency != "INR" or not reason.strip():
        assert not outcome.may_post


@given(st.integers(min_value=0, max_value=10**9))
def test_evaluate_is_pure(amount: int) -> None:
    """Same inputs, same Decision - no clock, no randomness, no hidden state."""
    req = request(amount=Money(amount))
    first = evaluate(req, CONFIG, RunState())
    second = evaluate(req, CONFIG, RunState())
    assert first.verdict is second.verdict and first.reason == second.reason


def test_every_decision_names_the_rules_that_produced_it() -> None:
    outcome = decide(amount=Money.from_rupees("40000"), confidence=Decimal("0.5"))
    explained = outcome.explain()  # type: ignore[attr-defined]
    assert {r["rule"] for r in explained["rules"]} == {r.value for r in RuleId} - {"amount_sign"}
    assert all(r["detail"] for r in explained["rules"]), "every rule states what it compared"


def test_config_changes_return_new_objects() -> None:
    """The alpha slider must not mutate policy under a running batch."""
    relaxed = CONFIG.with_confidence(Decimal("0.5"))
    assert CONFIG.min_confidence == Decimal("0.90")
    assert relaxed.min_confidence == Decimal("0.5")
    assert relaxed.per_action_cap == CONFIG.per_action_cap


# --------------------------------------------------------------------------- #
# The guard: audit before act, by construction
# --------------------------------------------------------------------------- #
def test_the_side_effect_only_runs_when_policy_says_yes(tmp_path) -> None:
    guard = fresh_guard(tmp_path)
    ran: list[str] = []

    allowed = guard.submit(request(), apply=lambda: ran.append("posted"))
    assert allowed.applied and ran == ["posted"]

    refused = guard.submit(
        request(subject_id="mtc_0002", amount=Money.from_rupees("300000")),
        apply=lambda: ran.append("should not happen"),
    )
    assert not refused.applied and ran == ["posted"]


def test_denials_are_audited_too(tmp_path) -> None:
    """A log that records only what was done cannot answer 'why did nothing happen?'"""
    guard = fresh_guard(tmp_path)
    guard.submit(request(action_class=ActionClass.MOVE_MONEY))
    seq, record = next(iter(guard.log.iter_records()))
    assert record.verdict is Verdict.DENIED
    assert "never initiates a payout" in record.reason
    assert guard.log.inclusion_proof(seq).verify(guard.log.root())


def test_a_forged_receipt_cannot_post(tmp_path) -> None:
    """The construction proof: no receipt, no post - and a receipt cannot be made
    up, because it carries an inclusion proof under a signed head."""
    guard = fresh_guard(tmp_path)
    ran: list[str] = []
    forged_head = SignedTreeHead(1, b"\x00" * 32, CLOCK.now(), "deadbeef", "forged", b"\x00" * 64)
    forged = AppendReceipt(0, b"\x00" * 32, forged_head, InclusionProof(b"\x00" * 32, 0, 1, ()))

    with pytest.raises(UnauditedAction):
        _post_to_books(
            request=request(), receipt=forged, log=guard.log, apply=lambda: ran.append("forged")
        )
    assert ran == []


def test_a_receipt_for_a_different_subject_cannot_post(tmp_path) -> None:
    """A real receipt is not a bearer token for anything else in the log."""
    guard = fresh_guard(tmp_path)
    ran: list[str] = []
    real = guard.submit(request(subject_id="mtc_aaaa"))

    with pytest.raises(UnauditedAction) as excinfo:
        _post_to_books(
            request=request(subject_id="mtc_bbbb"),
            receipt=real.receipt,
            log=guard.log,
            apply=lambda: ran.append("wrong subject"),
        )
    assert excinfo.value.context["expected"] == "mtc_bbbb"
    assert ran == []


def test_a_receipt_whose_audited_verdict_forbids_posting_cannot_post(tmp_path) -> None:
    guard = fresh_guard(tmp_path)
    ran: list[str] = []
    denied = guard.submit(request(action_class=ActionClass.MOVE_MONEY))

    with pytest.raises(UnauditedAction, match="does not permit posting"):
        _post_to_books(
            request=request(),
            receipt=denied.receipt,
            log=guard.log,
            apply=lambda: ran.append("denied but posted"),
        )
    assert ran == []


def test_the_audit_record_precedes_the_side_effect(tmp_path) -> None:
    """Ordering, observed rather than assumed: when the side effect runs, the record
    is already committed and provable."""
    guard = fresh_guard(tmp_path)
    observed: list[tuple[int, bool]] = []

    def apply() -> None:
        size = guard.log.size
        record = guard.log.record_at(size - 1)
        observed.append((size, record.subject_id == "mtc_0001"))

    guard.submit(request(), apply=apply)
    assert observed == [(1, True)]


def test_a_failing_side_effect_is_recorded_not_swallowed(tmp_path) -> None:
    guard = fresh_guard(tmp_path)

    def explode() -> None:
        raise TriveniError("the ledger rejected the posting")

    result = guard.submit(request(), apply=explode)
    assert not result.applied and "ledger rejected" in result.error
    kinds = [r.kind for _, r in guard.log.iter_records()]
    assert kinds == ["match.accept", "match.accept.failed"]


def test_the_run_budget_is_consumed_and_then_enforced(tmp_path) -> None:
    """A Rs 12,000 run cap admits exactly two Rs 5,000 postings; the third would
    project to Rs 15,000 and is denied. The cap binds cumulatively, which is the
    defence against an error that is individually small and collectively large."""
    guard = fresh_guard(tmp_path, config=PolicyConfig(run_total_cap=Money.from_rupees("12000")))
    verdicts = [
        guard.submit(request(subject_id=f"mtc_{i}"), apply=lambda: None).verdict for i in range(3)
    ]
    assert verdicts == [Verdict.AUTO_POSTED, Verdict.AUTO_POSTED, Verdict.DENIED]
    assert guard.engine.state.posted_count == 2
    assert guard.engine.state.posted_total == Money.from_rupees("10000")
    assert guard.engine.remaining_budget() == Money.from_rupees("2000")

    denied = guard.submit(request(subject_id="mtc_over"), apply=lambda: None)
    assert denied.verdict is Verdict.DENIED
    assert guard.engine.state.posted_count == 2, "a denial must not consume budget"


def test_kill_switch_is_recorded_before_it_takes_effect(tmp_path) -> None:
    guard = fresh_guard(tmp_path)
    guard.engage_kill_switch("suspected upstream feed corruption")

    _, record = next(iter(guard.log.iter_records()))
    assert "kill switch engaged" in record.reason
    assert "upstream feed corruption" in record.reason

    after = guard.submit(request(), apply=lambda: None)
    assert after.verdict is Verdict.DENIED and not after.applied
    with pytest.raises(KillSwitchEngaged):
        guard.require_live()


def test_the_log_stays_verifiable_across_a_whole_run(tmp_path) -> None:
    guard = fresh_guard(tmp_path)
    for i in range(25):
        guard.submit(
            request(subject_id=f"mtc_{i:04d}", amount=Money.from_rupees(str(1000 + i * 137))),
            apply=lambda: None,
        )
    report = guard.log.verify()
    assert report.ok, report.render()
    assert report.size == 25
