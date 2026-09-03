"""Triveni's own MCP server: a finance controller another agent can adopt in one call.

The Razorpay adapter is Triveni *consuming* MCP. This is Triveni *exposing* it. Six
tools, each returning structured JSON with a mandatory ``reason`` field, so an agent
that calls them gets not just an answer but the evidence for it:

    close_books(date)                    reconcile a day, return the close report
    explain_settlement(settlement_id)    the waterfall, itemised, balanced to Rs 0
    list_exceptions(filter)              what needs a human, and why
    resolve_exception(id, decision, ...) record a human decision, audited
    forecast_cash(horizon)               cash actually landing, with intervals
    verify_audit(from_root, to_root)     prove the log was appended to, not rewritten

**Every tool is read-or-propose.** `resolve_exception` is the only one that changes
state, it records a *human's* decision rather than making one, and it goes through the
same guarded wrapper as everything else - so it cannot post without an audit record.
There is no tool here that moves money, and there is no tool here that could be talked
into it, because the capability does not exist to be talked into.

**Why the `reason` field is mandatory on every response.** An agent composing Triveni
into a larger workflow needs to be able to explain what it did to *its* user. A tool
that returns `{"matched": 214}` makes that impossible; one that returns the same number
with "214 of 231 claims matched; 17 routed to review because their confidence fell
below the conformal threshold of 0.9548" makes it trivial.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from core.clock import Clock, FrozenClock, parse_ist
from core.money import Money, format_inr
from recon.exceptions import ExceptionType

ROOT: Final = Path(__file__).resolve().parent.parent

#: The MCP tool contract. Kept as data so it can be served over the wire, rendered in
#: the README, and asserted by a test without three copies drifting apart.
TOOLS: Final[list[dict[str, Any]]] = [
    {
        "name": "close_books",
        "description": (
            "Reconcile a merchant's gateway, bank and ledger for one day. Returns the "
            "match rate, the exceptions that need a human, the conformal guarantee in "
            "force, and the Merkle root of the decisions taken."
        ),
        "mutates": False,
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "ISO date, e.g. 2026-03-31"},
                "alpha": {
                    "type": "number",
                    "description": "error bound for unsupervised posting; default 0.01",
                },
            },
            "required": ["date"],
        },
    },
    {
        "name": "explain_settlement",
        "description": (
            "Itemise the difference between what a settlement grossed and what the "
            "bank credited: MDR, GST on the fee, TDS 194-O, refunds netted, holds. "
            "Balances to Rs 0 or reports the unexplained remainder as a finding."
        ),
        "mutates": False,
        "inputSchema": {
            "type": "object",
            "properties": {"settlement_id": {"type": "string"}},
            "required": ["settlement_id"],
        },
    },
    {
        "name": "list_exceptions",
        "description": (
            "What needs a human, typed into a closed 16-category taxonomy, each with "
            "its evidence bundle and the reason the system stopped."
        ),
        "mutates": False,
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "description": "one ExceptionType value"},
                "severity": {"type": "string", "enum": ["info", "low", "medium", "high"]},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "resolve_exception",
        "description": (
            "Record a HUMAN's decision on an exception. Triveni does not decide here; "
            "it writes the decision to the append-only audit log with the approver's "
            "name and reason, and returns the inclusion proof."
        ),
        "mutates": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "exception_id": {"type": "string"},
                "decision": {"type": "string", "enum": ["accept", "reject", "defer"]},
                "reason": {"type": "string"},
                "approver": {"type": "string"},
            },
            "required": ["exception_id", "decision", "reason", "approver"],
        },
    },
    {
        "name": "forecast_cash",
        "description": (
            "Cash actually landing per day over a horizon, with conformal prediction "
            "intervals whose empirical coverage is reported beside the nominal level."
        ),
        "mutates": False,
        "inputSchema": {
            "type": "object",
            "properties": {"horizon_days": {"type": "integer", "description": "default 14"}},
        },
    },
    {
        "name": "ask",
        "description": (
            "Answer a question about the reconciled books. Compiles to one of a fixed "
            "set of hand-written queries over three documented views, executes it, and "
            "verifies that every numeral in the answer appears in the result. Abstains "
            "rather than answering when it cannot - which is a normal outcome."
        ),
        "mutates": False,
        "inputSchema": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    },
    {
        "name": "verify_audit",
        "description": (
            "Prove the decision log was appended to and not rewritten, between two "
            "tree sizes. Returns the RFC 6962 consistency proof and the signed head."
        ),
        "mutates": False,
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_size": {"type": "integer"},
                "to_size": {"type": "integer"},
            },
        },
    },
]

WRITE_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    tool["name"] for tool in TOOLS if tool["mutates"]
)


@dataclass(slots=True)
class TriveniMCP:
    """The server. Pure functions over a reconciliation result, plus one audited write."""

    directory: Path = field(default_factory=lambda: ROOT / "data" / "seed")
    clock: Clock = field(default_factory=lambda: FrozenClock.at("2026-04-01 09:00"))
    _cached: Any = None
    _warehouse: Any = None

    # --- discovery ---------------------------------------------------------
    def list_tools(self) -> list[dict[str, Any]]:
        return [dict(tool) for tool in TOOLS]

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Dispatch. An unknown tool is refused rather than guessed at."""
        arguments = arguments or {}
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return _error(f"no such tool: {name}", known=[t["name"] for t in TOOLS])
        try:
            return handler(**arguments)
        except TypeError as exc:
            return _error(f"bad arguments for {name}: {exc}")

    # --- the run -----------------------------------------------------------
    def _result(self) -> Any:
        if self._cached is None:
            from recon.pipeline import reconcile

            self._cached = reconcile(directory=self.directory, clock=self.clock)
        return self._cached

    # --- tools -------------------------------------------------------------
    def _tool_close_books(self, date: str, alpha: float | str = 0.01) -> dict[str, Any]:
        from scripts.eval_pipeline import evaluate_pipeline

        as_of = parse_ist(date).date()
        result = self._result()
        report = evaluate_pipeline(alpha=Decimal(str(alpha)))
        metrics = {m.name: m for m in report.metrics}

        matched = int(metrics["match_rate"].value * len(result.rows))
        threshold = metrics.get("conformal_threshold")
        return {
            "ok": True,
            "as_of": str(as_of),
            "rows_ingested": len(result.rows),
            "matches": len(result.matches),
            "rows_matched": matched,
            "exceptions": len(result.exceptions),
            "match_rate": str(metrics["match_rate"].value),
            "auto_post_coverage": str(metrics["auto_post_coverage"].value),
            "conformal_threshold": str(threshold.value) if threshold else None,
            "nominal_bound": str(alpha),
            "realised_error_holdout": str(metrics["conformal_realised_error"].value),
            "unexplained": format_inr(Money(int(metrics["unexplained_amount"].value))),
            "stages": [s.canonical() for s in result.stages],
            "reason": (
                f"{len(result.matches)} match group(s) across {len(result.rows)} rows; "
                f"{len(result.exceptions)} routed to a human. Claims at or above "
                f"confidence {threshold.value if threshold else 'n/a'} post "
                f"unsupervised under a {alpha} error bound whose realised error on "
                f"held-out data was {metrics['conformal_realised_error'].value}."
            ),
        }

    def _tool_explain_settlement(self, settlement_id: str) -> dict[str, Any]:
        result = self._result()
        for row_id, waterfall in sorted(result.waterfalls.items()):
            if waterfall.settlement_id != settlement_id and row_id != settlement_id:
                continue
            return {
                "ok": True,
                "settlement_id": waterfall.settlement_id,
                "waterfall": waterfall.canonical(),
                "rendered": waterfall.render(),
                "balanced": waterfall.balanced,
                "reason": (
                    f"gross {format_inr(waterfall.gross)} less "
                    f"{len(waterfall.lines)} named component(s) gives "
                    f"{format_inr(waterfall.net_expected)}, against "
                    f"{format_inr(waterfall.net_received)} received - "
                    + (
                        "balanced to Rs 0."
                        if waterfall.balanced
                        else f"{format_inr(waterfall.residual)} unexplained, raised as a finding."
                    )
                ),
            }
        return _error(
            f"no settlement {settlement_id!r} in this run",
            known=sorted({w.settlement_id for w in result.waterfalls.values()})[:10],
        )

    def _tool_list_exceptions(
        self, type: str = "", severity: str = "", limit: int = 50
    ) -> dict[str, Any]:
        result = self._result()
        items = sorted(result.exceptions, key=lambda e: (e.severity.value, e.exception_id))
        if type:
            items = [e for e in items if e.exception_type.value == type]
        if severity:
            items = [e for e in items if e.severity.value == severity]
        shown = items[:limit]
        by_type: dict[str, int] = {}
        for exception in items:
            by_type[exception.exception_type.value] = (
                by_type.get(exception.exception_type.value, 0) + 1
            )
        return {
            "ok": True,
            "total": len(items),
            "by_type": dict(sorted(by_type.items())),
            "exceptions": [e.canonical() for e in shown],
            "reason": (
                f"{len(items)} exception(s) need a human"
                + (f" of type {type}" if type else "")
                + "; the largest group is "
                + (
                    f"{max(by_type, key=lambda k: by_type[k])} ({max(by_type.values())})"
                    if by_type
                    else "none"
                )
                + ". Each carries its evidence bundle and the reason the system stopped."
            ),
        }

    def _tool_resolve_exception(
        self, exception_id: str, decision: str, reason: str, approver: str
    ) -> dict[str, Any]:
        """Record a human's decision. Triveni does not decide here.

        Goes through the same guarded wrapper as every other books-affecting action,
        so it is impossible to record one without an audit entry - and the inclusion
        proof comes back with the response so the caller can verify it themselves.
        """
        from core.audit.log import AuditLog
        from core.guard import Guard
        from core.policy import ActionClass, ActionRequest, PolicyEngine

        if decision not in {"accept", "reject", "defer"}:
            return _error(f"decision must be accept, reject or defer, not {decision!r}")
        if not reason.strip() or not approver.strip():
            return _error("a human decision needs both an approver and a reason")

        result = self._result()
        match = next((e for e in result.exceptions if e.exception_id == exception_id), None)
        if match is None:
            return _error(f"no exception {exception_id!r} in this run")

        log = AuditLog(ROOT / ".triveni" / "audit.db", clock=self.clock)
        guard = Guard(log=log, engine=PolicyEngine(), clock=self.clock, run_id="mcp")
        outcome = guard.submit(
            ActionRequest(
                subject_id=exception_id,
                action_class=ActionClass.POST_TO_BOOKS
                if decision == "accept"
                else ActionClass.PROPOSE,
                amount=match.amount,
                confidence=Decimal(1),
                reason=f"{decision} by {approver}: {reason}",
                kind=f"exception.{decision}",
                evidence_count=match.evidence.count(),
                human_approved=decision == "accept",
                approver=approver,
            ),
            inputs={"exception_type": match.exception_type.value},
        )
        proof = outcome.receipt.proof
        head = outcome.receipt.head
        log.close()

        return {
            "ok": True,
            "exception_id": exception_id,
            "decision": decision,
            "verdict": outcome.verdict.value,
            "audit_index": outcome.receipt.seq,
            "merkle_root": head.root.hex(),
            "inclusion_proof": proof.to_json(),
            "reason": (
                f"{decision} recorded for {approver} at audit index "
                f"{outcome.receipt.seq}; the inclusion proof is returned so you can "
                f"verify it without trusting this response. Policy said: {outcome.reason}"
            ),
        }

    def _tool_forecast_cash(self, horizon_days: int = 14) -> dict[str, Any]:
        try:
            from forecast.service import forecast_cash
        except ImportError:
            return {
                "ok": False,
                "available": False,
                "reason": (
                    "cash forecasting lands at M14; this tool is declared so an agent "
                    "can discover the contract, and it reports its own absence rather "
                    "than returning a fabricated series."
                ),
            }
        return forecast_cash(directory=self.directory, horizon_days=horizon_days)

    def _tool_ask(self, question: str) -> dict[str, Any]:
        """Grounded Q&A. Every figure traced to a row, or no figure at all."""
        from qa.narrate import answer_question
        from qa.warehouse import build as build_warehouse

        if getattr(self, "_warehouse", None) is None:
            self._warehouse = build_warehouse(self._result())
        answer = answer_question(question, self._warehouse)
        payload = answer.canonical()
        payload["ok"] = True
        payload["rows"] = [
            dict(zip(answer.columns, row, strict=True)) for row in answer.rows[:20]
        ]
        payload["reason"] = (
            f"{'answered' if answer.answered else 'abstained'}: {answer.reason}"
        )
        return payload

    def _tool_verify_audit(self, from_size: int = 0, to_size: int = 0) -> dict[str, Any]:
        from core.audit.log import AuditLog

        path = ROOT / ".triveni" / "audit.db"
        if not path.exists():
            return _error("no audit log yet; run close_books or make demo first")

        log = AuditLog(path, clock=self.clock)
        try:
            size = log.size
            to_size = to_size or size
            from_size = from_size or max(size // 2, 1)
            report = log.verify()
            proof = log.consistency_proof(from_size, to_size)
            verified = proof.verify(log.root(from_size), log.root(to_size))
            head = log.latest_head()
        finally:
            log.close()

        return {
            "ok": report.ok and verified,
            "size": size,
            "root": report.root.hex(),
            "signed_head": head.to_json(),
            "consistency": {**proof.to_json(), "verified": verified},
            "checks": [{"name": n, "passed": p, "detail": d} for n, p, d in report.checks],
            "tampered_index": report.tampered_index,
            "reason": (
                f"the size-{from_size} log is a prefix of the size-{to_size} log: "
                f"{'proved' if verified else 'PROOF FAILED'} with "
                f"{len(proof.path)} hash(es). This is the RFC 6962 construction that "
                f"secures the web PKI, applied to a financial decision log."
            )
            if verified
            else (
                f"consistency proof FAILED between sizes {from_size} and {to_size}"
                + (
                    f"; the log was altered at index {report.tampered_index}"
                    if report.tampered_index is not None
                    else ""
                )
            ),
        }


def _error(message: str, **context: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, "reason": message, **context}


def adoption_snippet() -> str:
    """The ten lines that go in the README: another agent adopting Triveni."""
    return """\
from api.mcp_server import TriveniMCP

triveni = TriveniMCP()                               # offline, no credentials
close = triveni.call("close_books", {"date": "2026-03-31"})

print(close["reason"])                               # every response explains itself
print(close["conformal_threshold"], close["realised_error_holdout"])

for exception in triveni.call("list_exceptions", {"severity": "high"})["exceptions"]:
    print(exception["type"], exception["reason"])

print(triveni.call("verify_audit", {})["reason"])    # the books prove themselves
"""


def describe_boundary() -> str:
    return (
        f"Triveni exposes {len(TOOLS)} tools, of which {len(WRITE_TOOL_NAMES)} mutates "
        f"state - `resolve_exception`, which records a *human's* decision through the "
        f"guarded wrapper and returns the audit inclusion proof. No tool moves money."
    )


def as_of_default() -> dt.date:
    return dt.date(2026, 3, 31)


#: Re-exported so an agent can enumerate the whole taxonomy from a single import.
TAXONOMY: Final[list[str]] = [t.value for t in ExceptionType]
