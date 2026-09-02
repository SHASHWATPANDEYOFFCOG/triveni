"""Typed error hierarchy. No bare `except` anywhere in Triveni; every failure that
business logic can produce has a name, and every name carries the evidence needed to
explain it to a human.
"""

from __future__ import annotations

from typing import Any


class TriveniError(Exception):
    """Root of every Triveni error. Carries a structured context bag so that a
    failure can be logged, audited and rendered without string-parsing."""

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = context

    def __str__(self) -> str:
        if not self.context:
            return self.message
        detail = " ".join(f"{k}={v!r}" for k, v in sorted(self.context.items()))
        return f"{self.message} ({detail})"


# --- money -----------------------------------------------------------------
class MoneyError(TriveniError):
    """Anything that would let imprecision into a money path."""


class FloatInMoneyPath(MoneyError):
    """A float was offered where integer paise are required. This is always a bug:
    binary floating point cannot represent 0.01 exactly, and a reconciliation system
    that is off by a paise is a reconciliation system that does not reconcile."""


class CurrencyMismatch(MoneyError):
    """Arithmetic across two currencies. Convert explicitly through an FX rate."""


class NegativeAllocation(MoneyError):
    """Allocation weights must be non-negative and not all zero."""


# --- time ------------------------------------------------------------------
class ClockError(TriveniError):
    """Time handling failure."""


class NaiveDatetime(ClockError):
    """A datetime without a timezone reached the ledger. Every instant in Triveni is
    timezone-aware; a naive datetime is an ambiguity waiting to become a mismatch."""


# --- ingest / canonicalisation ---------------------------------------------
class IngestError(TriveniError):
    """A source row could not be read."""


class CorruptRow(IngestError):
    """A row is malformed. Never fatal: it becomes a typed `corrupt_row` exception
    with its raw payload attached as evidence, and the batch continues."""


# --- audit -----------------------------------------------------------------
class AuditError(TriveniError):
    """Audit log failure."""


class TamperDetected(AuditError):
    """A stored leaf no longer hashes to the value the tree committed to."""


class UnauditedAction(AuditError):
    """An attempt to post to the books without first writing an audit record.
    Invariant D.1.5: audit before act, enforced by construction."""


# --- policy / gating -------------------------------------------------------
class PolicyError(TriveniError):
    """Policy engine failure."""


class PolicyViolation(PolicyError):
    """A bounded action tried to exceed a hard cap. Deterministic code refuses; no
    model can talk its way past this."""


class KillSwitchEngaged(PolicyError):
    """The global kill switch is on: every books-affecting action is refused."""


# --- llm -------------------------------------------------------------------
class LLMError(TriveniError):
    """LLM gateway failure."""


class BudgetExhausted(LLMError):
    """The per-run rupee budget is spent. Triveni abstains rather than overspending."""


class SchemaViolation(LLMError):
    """The model returned something that is not valid against the declared schema,
    after retries. The row abstains into a typed exception."""


class PromptInjectionDetected(LLMError):
    """Untrusted source text tried to issue instructions. Denied and logged."""


class CassetteMiss(LLMError):
    """Offline replay was asked for a call that was never recorded. Fails loudly:
    a silent live call would break the offline guarantee."""


# --- solving ---------------------------------------------------------------
class SolverError(TriveniError):
    """Optimisation failure."""


class InfeasibleAssignment(SolverError):
    """The constraint set admits no assignment. Routes to human triage."""


class InvariantViolation(TriveniError):
    """A hard invariant from PART D.1 does not hold. Always a build failure."""
