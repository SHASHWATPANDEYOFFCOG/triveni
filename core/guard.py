"""The guarded action wrapper: audit before act, enforced by construction.

Invariant D.1.5 says it must be *impossible* to post a match without an audit record.
A naming convention does not achieve that, and neither does a code review. What does:

``_post_to_books`` is module-private and demands an :class:`AppendReceipt`. A receipt
carries a Merkle inclusion proof against an Ed25519-signed tree head, and the guard
verifies it - the proof, the signature, and that the audited record is about *this*
subject and carries a verdict that permits posting. A caller who has not written to
the log cannot manufacture one of those, because doing so would require forging a
signature.

So the ordering is not a policy we follow. It is a precondition the type system asks
for and cryptography enforces:

    request -> policy.evaluate -> audit.append -> receipt -> verify receipt -> act

Denials are audited too. A system that only logs what it did, and not what it refused
to do, cannot answer "why did nothing happen on the 14th?" - which is exactly the
question an auditor asks.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.audit.log import AppendReceipt, AuditLog, Verdict, new_decision
from core.clock import Clock
from core.errors import KillSwitchEngaged, TriveniError, UnauditedAction
from core.money import format_inr
from core.policy import ActionClass, ActionRequest, Decision, PolicyEngine


@dataclass(frozen=True, slots=True)
class GuardResult[T]:
    """What happened, why, and the proof it was recorded before it happened."""

    decision: Decision
    receipt: AppendReceipt
    applied: bool
    value: T | None = None
    error: str = ""

    @property
    def verdict(self) -> Verdict:
        return self.decision.verdict

    @property
    def reason(self) -> str:
        return self.decision.reason

    def audit_index(self) -> int:
        return self.receipt.seq


def _post_to_books[T](
    *,
    request: ActionRequest,
    receipt: AppendReceipt,
    log: AuditLog,
    apply: Callable[[], T],
) -> T:
    """The ONLY function permitted to effect a books-affecting change.

    Module-private and receipt-gated. Every check below is about one question: does
    the caller hold cryptographic proof that this exact action was recorded, as
    permitted, before now?
    """
    if not receipt.verify():
        raise UnauditedAction(
            "audit receipt does not prove inclusion under the signed root",
            subject_id=request.subject_id,
            seq=receipt.seq,
        )
    if not receipt.head.verify_signature(log.key.public):
        raise UnauditedAction(
            "audit receipt carries an unsigned or forged tree head",
            subject_id=request.subject_id,
            key_id=receipt.head.key_id,
        )

    audited = log.record_at(receipt.seq)
    if audited.subject_id != request.subject_id:
        raise UnauditedAction(
            "audit receipt is for a different subject",
            expected=request.subject_id,
            found=audited.subject_id,
        )
    if audited.verdict not in (Verdict.AUTO_POSTED, Verdict.APPROVED_BY_HUMAN):
        raise UnauditedAction(
            "the audited decision does not permit posting",
            subject_id=request.subject_id,
            verdict=audited.verdict.value,
        )
    return apply()


@dataclass(slots=True)
class Guard:
    """The one door between a proposal and the books."""

    log: AuditLog
    engine: PolicyEngine
    clock: Clock
    run_id: str = ""

    def submit[T](
        self,
        request: ActionRequest,
        apply: Callable[[], T] | None = None,
        *,
        inputs: dict[str, Any] | None = None,
    ) -> GuardResult[T]:
        """Evaluate, record, and only then - if policy allowed it - act.

        ``apply`` is the side effect. It is never called before the audit write, and
        never called at all unless the receipt verifies.
        """
        decision = self.engine.decide(request)

        record = new_decision(
            kind=request.kind,
            subject_id=request.subject_id,
            verdict=decision.verdict,
            reason=decision.reason,
            clock=self.clock,
            inputs=dict(inputs or {}, amount=request.amount.paise, currency=request.amount.currency),
            action={
                "class": request.action_class.value,
                "amount_display": format_inr(request.amount),
                "confidence": str(request.confidence),
            },
            outcome={"applied": False},
            evidence=decision.explain(),
            actor=request.approver or "triveni",
            run_id=self.run_id,
        )
        receipt = self.log.append(record)

        if not decision.may_post or apply is None:
            return GuardResult(decision=decision, receipt=receipt, applied=False)

        try:
            value = _post_to_books(request=request, receipt=receipt, log=self.log, apply=apply)
        except TriveniError as exc:
            # The failure is itself a decision worth recording: something got past
            # policy and then could not be applied, and an auditor needs to see it.
            self.log.append(
                new_decision(
                    kind=f"{request.kind}.failed",
                    subject_id=request.subject_id,
                    verdict=Verdict.DENIED,
                    reason=f"application failed after approval: {exc}",
                    clock=self.clock,
                    inputs={"audit_seq": receipt.seq},
                    outcome={"applied": False, "error": type(exc).__name__},
                    run_id=self.run_id,
                )
            )
            return GuardResult(
                decision=decision, receipt=receipt, applied=False, error=str(exc)
            )

        self.engine.commit(decision)
        return GuardResult(decision=decision, receipt=receipt, applied=True, value=value)

    def observe(self, subject_id: str, reason: str, **inputs: Any) -> GuardResult[None]:
        """Record something that happened without changing the books - an ingest, a
        stage report, an abstention. Keeps the log a complete narrative."""
        from decimal import Decimal

        from core.money import Money

        return self.submit(
            ActionRequest(
                subject_id=subject_id,
                action_class=ActionClass.READ,
                amount=Money.zero(),
                confidence=Decimal(1),
                reason=reason,
                kind="observation",
            ),
            inputs=inputs,
        )

    def engage_kill_switch(self, reason: str = "operator request") -> GuardResult[None]:
        """Stop the world. Recorded first, so the log shows when and why."""
        result = self.observe("system", f"kill switch engaged: {reason}")
        self.engine.engage_kill_switch(reason)
        return result

    def require_live(self) -> None:
        if self.engine.config.kill_switch:
            raise KillSwitchEngaged("kill switch is engaged; refusing all books-affecting work")
