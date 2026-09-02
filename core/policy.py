"""The policy engine: pure, deterministic, and the only thing allowed to say yes.

This module is where non-negotiables 2 and 3 - BOUNDED and GATED - actually live.
Everything here is a pure function of ``(config, state, request)``. No clock, no
database, no network, no model. That is the point: the limits on what Triveni may do
must be enforced by code that no model can reason its way past, and that a reviewer
can read in one sitting and be sure of.

Three ideas do the work.

**Hard caps are hard.** A cap is not advice. If a proposed posting exceeds the
per-action cap, the run total, or the count budget, the verdict is ``DENIED`` and no
amount of model confidence changes that.

**Materiality gates, confidence gates.** Below the materiality threshold and above
the calibrated confidence bound, a match may auto-post. Above materiality *or* below
the bound, it goes to a human. The confidence bound is not a guessed 0.7 - it is the
conformal threshold computed in ``core/conformal.py`` and handed in here (M12).

**Some things are never allowed.** ``ActionClass.MOVE_MONEY`` is refused
unconditionally and is not configurable, because a finance controller reads and
proposes; it does not move money. There is no flag to turn that off, which is the
only kind of promise worth making.

Every outcome carries the list of rules that fired and a reason string assembled from
them, so the audit log records *why* - not just what.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from typing import Final

from core.audit.log import Verdict
from core.money import INR, Money, format_inr

# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


class ActionClass(StrEnum):
    """What a proposed action would actually do. Ordered by blast radius."""

    READ = "read"
    """Look at data. Always allowed."""

    PROPOSE = "propose"
    """Suggest something to a human. Always allowed; changes nothing."""

    POST_TO_BOOKS = "post_to_books"
    """Record a match or an adjustment in the merchant's books. Gated."""

    MOVE_MONEY = "move_money"
    """Initiate a payout, refund, or transfer. NEVER allowed. See RULE_NO_MONEY_MOVEMENT."""


class RuleId(StrEnum):
    """Closed set of policy rules. A rule that fires is named, never anonymous."""

    KILL_SWITCH = "kill_switch"
    NO_MONEY_MOVEMENT = "no_money_movement"
    CURRENCY_ALLOWED = "currency_allowed"
    AMOUNT_SIGN = "amount_sign"
    PER_ACTION_CAP = "per_action_cap"
    RUN_TOTAL_CAP = "run_total_cap"
    RUN_COUNT_CAP = "run_count_cap"
    MATERIALITY = "materiality"
    CONFIDENCE = "confidence"
    EVIDENCE_PRESENT = "evidence_present"
    REASON_PRESENT = "reason_present"


@dataclass(frozen=True, slots=True)
class ActionRequest:
    """A proposed books-affecting action, before anyone has agreed to it."""

    subject_id: str
    action_class: ActionClass
    amount: Money
    confidence: Decimal
    reason: str
    kind: str = "match.accept"
    evidence_count: int = 0
    human_approved: bool = False
    approver: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.confidence, Decimal):
            raise TypeError("confidence must be a Decimal - see core/money.py on floats")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class PolicyConfig:
    """The limits. Every field is a hard number a reviewer can check.

    The defaults are deliberately conservative for an SMB doing a few lakh a day:
    auto-post only small, high-confidence, well-evidenced matches, and put everything
    else in front of a person. The alpha slider in the UI moves ``min_confidence``;
    it cannot move the caps.
    """

    kill_switch: bool = False

    #: Above this, a human must approve however confident the system is. This is the
    #: materiality threshold an auditor would recognise, expressed in paise.
    materiality: Money = field(default_factory=lambda: Money.from_rupees("25000"))

    #: No single auto-posted action may exceed this, ever.
    per_action_cap: Money = field(default_factory=lambda: Money.from_rupees("200000"))

    #: Nor may one run auto-post more than this in total.
    run_total_cap: Money = field(default_factory=lambda: Money.from_rupees("5000000"))

    #: Nor more than this many actions - a defence against a systematic error that
    #: is individually small and collectively catastrophic.
    run_count_cap: int = 5_000

    #: The conformal threshold from core/conformal.py. Below it, route to a human.
    min_confidence: Decimal = Decimal("0.90")

    #: A match with no supporting evidence is not a match, it is a guess.
    min_evidence: int = 1

    allowed_currencies: frozenset[str] = frozenset({INR})

    def with_confidence(self, min_confidence: Decimal) -> PolicyConfig:
        """Used by the alpha slider. Returns a new config; nothing mutates."""
        return replace(self, min_confidence=min_confidence)

    def engaged_kill_switch(self) -> PolicyConfig:
        return replace(self, kill_switch=True)


@dataclass(frozen=True, slots=True)
class RunState:
    """What this run has already committed to. Immutable; every accept returns a new one."""

    posted_total: Money = field(default_factory=Money.zero)
    posted_count: int = 0

    def after_posting(self, amount: Money) -> RunState:
        return RunState(
            posted_total=self.posted_total + abs(amount),
            posted_count=self.posted_count + 1,
        )


# --------------------------------------------------------------------------- #
# Outcomes
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class RuleOutcome:
    """One rule's verdict, with the numbers it compared."""

    rule: RuleId
    passed: bool
    detail: str
    blocking: bool = True
    """A blocking rule that fails denies outright; a non-blocking one routes to a human."""

    def __str__(self) -> str:
        return f"{'ok' if self.passed else 'FAIL'} {self.rule.value}: {self.detail}"


@dataclass(frozen=True, slots=True)
class Decision:
    """What policy decided, why, and on the strength of which rules."""

    verdict: Verdict
    reason: str
    rules: tuple[RuleOutcome, ...]
    request: ActionRequest

    @property
    def may_post(self) -> bool:
        """The single question the guard asks. Note ``NEEDS_REVIEW`` is not a yes."""
        return self.verdict in (Verdict.AUTO_POSTED, Verdict.APPROVED_BY_HUMAN)

    @property
    def failed_rules(self) -> tuple[RuleOutcome, ...]:
        return tuple(r for r in self.rules if not r.passed)

    def explain(self) -> dict[str, object]:
        """The structure that goes into the audit record's ``evidence`` field."""
        return {
            "verdict": self.verdict.value,
            "reason": self.reason,
            "rules": [
                {"rule": r.rule.value, "passed": r.passed, "detail": r.detail, "blocking": r.blocking}
                for r in self.rules
            ],
        }


#: Documented, non-configurable: a finance controller reads and proposes.
RULE_NO_MONEY_MOVEMENT: Final = (
    "Triveni never initiates a payout, refund or transfer. This rule has no "
    "configuration flag and no override path."
)


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #
def evaluate(request: ActionRequest, config: PolicyConfig, state: RunState) -> Decision:
    """Decide whether ``request`` may proceed. Pure: same inputs, same Decision.

    Rules are evaluated in blast-radius order and *all* of them run, even after one
    fails, because the audit record should show the full picture rather than
    whichever rule happened to trip first.
    """
    rules: list[RuleOutcome] = []

    # --- absolute refusals -------------------------------------------------
    rules.append(
        RuleOutcome(
            RuleId.KILL_SWITCH,
            passed=not config.kill_switch,
            detail="kill switch engaged; all books-affecting actions refused"
            if config.kill_switch
            else "kill switch off",
        )
    )
    rules.append(
        RuleOutcome(
            RuleId.NO_MONEY_MOVEMENT,
            passed=request.action_class is not ActionClass.MOVE_MONEY,
            detail=RULE_NO_MONEY_MOVEMENT
            if request.action_class is ActionClass.MOVE_MONEY
            else "action does not move money",
        )
    )
    currency_ok = request.amount.currency in config.allowed_currencies
    rules.append(
        RuleOutcome(
            RuleId.CURRENCY_ALLOWED,
            passed=currency_ok,
            detail=f"{request.amount.currency} "
            + (
                "is permitted"
                if currency_ok
                else f"is not in {sorted(config.allowed_currencies)}"
            ),
        )
    )
    rules.append(
        RuleOutcome(
            RuleId.REASON_PRESENT,
            passed=bool(request.reason.strip()),
            detail="reason supplied" if request.reason.strip() else "no reason given",
        )
    )

    # --- hard caps ---------------------------------------------------------
    # Cap comparisons are only meaningful within one currency, and Money refuses to
    # compare across currencies by design. So when the currency rule has already
    # failed, the cap rules record that they could not be evaluated rather than
    # raising: a policy engine that can be crashed by hostile input is a policy
    # engine that can be bypassed by crashing it. The verdict is DENIED either way,
    # because CURRENCY_ALLOWED is blocking.
    magnitude = abs(request.amount)
    unevaluable = "not evaluated: amount is not in an allowed currency"

    within_action_cap = currency_ok and magnitude <= config.per_action_cap
    rules.append(
        RuleOutcome(
            RuleId.PER_ACTION_CAP,
            passed=within_action_cap,
            detail=(
                f"{format_inr(magnitude)} vs per-action cap {format_inr(config.per_action_cap)}"
                if currency_ok
                else unevaluable
            ),
        )
    )

    projected = state.posted_total + magnitude if currency_ok else state.posted_total
    within_run_cap = currency_ok and projected <= config.run_total_cap
    rules.append(
        RuleOutcome(
            RuleId.RUN_TOTAL_CAP,
            passed=within_run_cap,
            detail=(
                f"run total would reach {format_inr(projected)} vs cap "
                f"{format_inr(config.run_total_cap)}"
                if currency_ok
                else unevaluable
            ),
        )
    )

    within_count_cap = state.posted_count < config.run_count_cap
    rules.append(
        RuleOutcome(
            RuleId.RUN_COUNT_CAP,
            passed=within_count_cap,
            detail=f"{state.posted_count} posted this run vs cap {config.run_count_cap}",
        )
    )

    # --- gates (non-blocking: they route to a human rather than refusing) ---
    below_materiality = currency_ok and magnitude <= config.materiality
    rules.append(
        RuleOutcome(
            RuleId.MATERIALITY,
            passed=below_materiality,
            detail=(
                f"{format_inr(magnitude)} vs materiality threshold "
                f"{format_inr(config.materiality)}"
                if currency_ok
                else unevaluable
            ),
            blocking=False,
        )
    )

    confident_enough = request.confidence >= config.min_confidence
    rules.append(
        RuleOutcome(
            RuleId.CONFIDENCE,
            passed=confident_enough,
            detail=f"confidence {request.confidence} vs conformal threshold "
            f"{config.min_confidence}",
            blocking=False,
        )
    )

    enough_evidence = request.evidence_count >= config.min_evidence
    rules.append(
        RuleOutcome(
            RuleId.EVIDENCE_PRESENT,
            passed=enough_evidence,
            detail=f"{request.evidence_count} evidence item(s), minimum {config.min_evidence}",
            blocking=False,
        )
    )

    return _verdict(request, tuple(rules))


def _verdict(request: ActionRequest, rules: tuple[RuleOutcome, ...]) -> Decision:
    """Turn the rule outcomes into one verdict and one sentence."""
    blocking_failures = [r for r in rules if not r.passed and r.blocking]
    gate_failures = [r for r in rules if not r.passed and not r.blocking]

    if blocking_failures:
        reason = "denied: " + "; ".join(r.detail for r in blocking_failures)
        return Decision(Verdict.DENIED, reason, rules, request)

    # Reads and proposals change nothing, so the gates do not apply to them. The
    # caller's own reason is carried through verbatim rather than replaced by a
    # generic one: an observation whose reason is "kill switch engaged: suspected
    # upstream feed corruption" is the whole value of recording it.
    if request.action_class in (ActionClass.READ, ActionClass.PROPOSE):
        return Decision(
            Verdict.OBSERVED,
            f"{request.action_class.value}: {request.reason}",
            rules,
            request,
        )

    if request.human_approved:
        approver = request.approver or "unnamed approver"
        return Decision(
            Verdict.APPROVED_BY_HUMAN,
            f"approved by {approver}: {request.reason}",
            rules,
            request,
        )

    if gate_failures:
        reason = "needs review: " + "; ".join(r.detail for r in gate_failures)
        return Decision(Verdict.NEEDS_REVIEW, reason, rules, request)

    return Decision(
        Verdict.AUTO_POSTED,
        f"auto-posted: {request.reason}",
        rules,
        request,
    )


@dataclass(slots=True)
class PolicyEngine:
    """Stateful wrapper that accumulates the run budget across decisions.

    The decision logic itself stays in the pure :func:`evaluate`; this only tracks
    how much of the run's allowance has been spent, so that the 4,001st small match
    is refused once the count cap is reached.
    """

    config: PolicyConfig = field(default_factory=PolicyConfig)
    state: RunState = field(default_factory=RunState)
    kill_reason: str = ""

    def decide(self, request: ActionRequest) -> Decision:
        return evaluate(request, self.config, self.state)

    def commit(self, decision: Decision) -> None:
        """Record that a decision was acted on, consuming run budget.

        Called by the guard *after* the audit write, never by callers directly.
        """
        if decision.may_post:
            self.state = self.state.after_posting(decision.request.amount)

    def engage_kill_switch(self, reason: str = "operator request") -> None:
        """One flag that refuses every books-affecting action from here on."""
        self.config = self.config.engaged_kill_switch()
        self.kill_reason = reason

    def remaining_budget(self) -> Money:
        return self.config.run_total_cap - self.state.posted_total
