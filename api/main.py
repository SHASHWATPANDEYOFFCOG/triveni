"""Triveni's HTTP surface: typed, documented, and honest about what it will not do.

Everything here reads or proposes. The one endpoint that changes state records a
*human's* decision through the same guarded wrapper as the rest of the system, so it
cannot write to the books without an audit entry - and it hands back the Merkle
inclusion proof so the caller can verify that for themselves rather than trusting this
response.

The close endpoint streams. A reconciliation takes ten seconds on the seed and a minute
at scale, and a spinner for a minute is a worse product than a stage ladder that fills
in as the work happens - so `/close/stream` emits server-sent events per stage. SSE
rather than websockets because the traffic is one-directional and a plain
`StreamingResponse` needs no extra dependency.

No credential is required by any endpoint. `/health` says so explicitly, because "does
this need my Razorpay keys" is the first question anyone running it will have.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.mcp_server import TOOLS, TriveniMCP, adoption_snippet
from api.mcp_server import describe_boundary as mcp_boundary
from core.money import Money, format_inr
from ingest.adapters.razorpay_mcp import describe_boundary as rails_boundary
from ingest.adapters.razorpay_mcp import tool_inventory
from recon.exceptions import DESCRIPTIONS, ExceptionType

ROOT: Final = Path(__file__).resolve().parent.parent
TRIVENI_VERSION: Final = "0.1.0"

app = FastAPI(
    title="Triveni",
    version=TRIVENI_VERSION,
    description=(
        "The AI Finance Controller: three ledgers, one balanced set of books, with a "
        "guarantee. Every endpoint reads or proposes; none moves money."
    ),
)

_server = TriveniMCP()


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class Health(BaseModel):
    """Deliberately reports the operating mode, because 'do I need keys?' is the
    first question anyone running this will have."""

    status: str
    version: str
    rails: str
    llm_mode: str
    credentials_required: bool
    money_movement_possible: bool


class StageView(BaseModel):
    stage: str
    label: str
    matches_added: int
    rows_consumed: int
    elapsed_ms: Decimal
    detail: str


class CloseReport(BaseModel):
    as_of: str
    rows_ingested: int
    matches: int
    exceptions: int
    match_rate: str
    auto_post_coverage: str
    conformal_threshold: str | None
    nominal_bound: str
    realised_error_holdout: str
    unexplained: str
    reason: str = Field(description="Every response explains itself. Mandatory.")
    stages: list[StageView]


class ExceptionView(BaseModel):
    exception_id: str
    type: str
    severity: str
    reason: str
    amount: str
    suggested_action: str
    abstained_because: str
    evidence_count: int


class ResolveRequest(BaseModel):
    """A human's decision. Every field is required, including who and why."""

    decision: str = Field(pattern="^(accept|reject|defer)$")
    reason: str = Field(min_length=1)
    approver: str = Field(min_length=1)


# --------------------------------------------------------------------------- #
# Meta
# --------------------------------------------------------------------------- #
@app.get("/health", response_model=Health, tags=["meta"])
def health() -> Health:
    return Health(
        status="ok",
        version=TRIVENI_VERSION,
        rails=os.environ.get("TRIVENI_RAILS", "offline"),
        llm_mode=os.environ.get("TRIVENI_LLM_MODE", "replay"),
        credentials_required=False,
        money_movement_possible=False,
    )


@app.get("/", tags=["meta"])
def root() -> dict[str, Any]:
    return {
        "name": "Triveni",
        "tagline": "three ledgers, one balanced set of books, with a guarantee",
        "docs": "/docs",
        "health": "/health",
        "close": "/close",
        "stream": "/close/stream",
        "boundaries": {"razorpay": rails_boundary(), "mcp": mcp_boundary()},
    }


@app.get("/boundaries", tags=["meta"])
def boundaries() -> dict[str, Any]:
    """What Triveni may and may not do, generated from the code that enforces it.

    Not a policy document - the same frozensets the allowlist checks against.
    """
    return {
        "razorpay": {"summary": rails_boundary(), "tools": tool_inventory()},
        "mcp": {"summary": mcp_boundary(), "tools": TOOLS},
        "taxonomy": [
            {"type": t.value, "meaning": DESCRIPTIONS[t]} for t in ExceptionType
        ],
        "adoption_snippet": adoption_snippet(),
    }


# --------------------------------------------------------------------------- #
# The close
# --------------------------------------------------------------------------- #
@app.get("/close", response_model=CloseReport, tags=["close"])
def close_books(
    date: str = Query(default="2026-03-31", description="ISO date"),
    alpha: str = Query(default="0.01", description="unsupervised error bound"),
) -> CloseReport:
    payload = _server.call("close_books", {"date": date, "alpha": alpha})
    if not payload.get("ok"):
        raise HTTPException(status_code=400, detail=payload.get("reason", "close failed"))
    return CloseReport(
        as_of=payload["as_of"],
        rows_ingested=payload["rows_ingested"],
        matches=payload["matches"],
        exceptions=payload["exceptions"],
        match_rate=payload["match_rate"],
        auto_post_coverage=payload["auto_post_coverage"],
        conformal_threshold=payload["conformal_threshold"],
        nominal_bound=payload["nominal_bound"],
        realised_error_holdout=payload["realised_error_holdout"],
        unexplained=payload["unexplained"],
        reason=payload["reason"],
        stages=[StageView(**stage) for stage in payload["stages"]],
    )


@app.get("/close/stream", tags=["close"])
async def close_stream(date: str = Query(default="2026-03-31")) -> StreamingResponse:
    """Stream the close, stage by stage.

    A reconciliation takes ten seconds on the seed and a minute at scale. A spinner
    for a minute is a worse product than a ladder that fills in as the work happens,
    and the ladder is also the honest view: it shows which stage did what, so a slow
    run is diagnosable rather than merely slow.
    """

    async def events() -> AsyncIterator[str]:
        yield _sse("start", {"date": date, "message": "closing the books"})
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _server._result)

        for stage in result.stages:
            yield _sse(
                "stage",
                {
                    "stage": stage.stage,
                    "label": stage.label,
                    "matches_added": stage.matches_added,
                    "rows_consumed": stage.rows_consumed,
                    "elapsed_ms": str(stage.elapsed_ms),
                    "detail": stage.detail,
                },
            )
            await asyncio.sleep(0)

        by_type: dict[str, int] = {}
        for exception in result.exceptions:
            by_type[exception.exception_type.value] = (
                by_type.get(exception.exception_type.value, 0) + 1
            )
        yield _sse(
            "done",
            {
                "rows": len(result.rows),
                "matches": len(result.matches),
                "exceptions": len(result.exceptions),
                "by_type": dict(sorted(by_type.items())),
                "waterfalls_balanced": sum(1 for w in result.waterfalls.values() if w.balanced),
                "waterfalls": len(result.waterfalls),
            },
        )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------- #
# Exceptions and settlements
# --------------------------------------------------------------------------- #
@app.get("/exceptions", response_model=list[ExceptionView], tags=["triage"])
def list_exceptions(
    type: str = Query(default=""),
    severity: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[ExceptionView]:
    payload = _server.call(
        "list_exceptions", {"type": type, "severity": severity, "limit": limit}
    )
    return [
        ExceptionView(
            exception_id=item["exception_id"],
            type=item["type"],
            severity=item["severity"],
            reason=item["reason"],
            amount=format_inr(Money(item["amount"]["paise"])),
            suggested_action=item["suggested_action"],
            abstained_because=item["evidence"].get("abstained_because", ""),
            evidence_count=len(item["evidence"].get("items", [])),
        )
        for item in payload.get("exceptions", [])
    ]


@app.get("/settlements/{settlement_id}", tags=["explain"])
def explain_settlement(settlement_id: str) -> dict[str, Any]:
    payload = _server.call("explain_settlement", {"settlement_id": settlement_id})
    if not payload.get("ok"):
        raise HTTPException(status_code=404, detail=payload.get("reason", "not found"))
    return payload


@app.post("/exceptions/{exception_id}/resolve", tags=["triage"])
def resolve_exception(exception_id: str, request: ResolveRequest) -> dict[str, Any]:
    """Record a human's decision. Triveni does not decide here.

    Returns the audit index and the Merkle inclusion proof, so the caller can verify
    the record exists without trusting this response.
    """
    payload = _server.call(
        "resolve_exception",
        {
            "exception_id": exception_id,
            "decision": request.decision,
            "reason": request.reason,
            "approver": request.approver,
        },
    )
    if not payload.get("ok"):
        raise HTTPException(status_code=400, detail=payload.get("reason", "refused"))
    return payload


# --------------------------------------------------------------------------- #
# Grounded Q&A
# --------------------------------------------------------------------------- #
@app.get("/ask", tags=["explain"])
def ask(question: str = Query(description="a question about the reconciled books")) -> dict[str, Any]:
    """Answer from reconciled data, or abstain and show the table.

    An abstention is a 200, not an error: "I cannot answer that from the data" is a
    correct response, and returning it as a failure would push a caller toward
    retrying until something came back.
    """
    return _server.call("ask", {"question": question})


@app.get("/ask/catalogue", tags=["explain"])
def ask_catalogue() -> dict[str, Any]:
    """What can be asked, so the UI's empty state can teach rather than guess."""
    from qa.compile import catalogue as query_catalogue

    return {
        "answerable": query_catalogue(),
        "note": (
            "Triveni answers from three documented views. It selects a hand-written "
            "query rather than generating SQL, and every numeral in an answer is "
            "checked against the rows the query returned."
        ),
    }


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #
@app.get("/audit/verify", tags=["audit"])
def verify_audit(from_size: int = Query(default=0), to_size: int = Query(default=0)) -> dict[str, Any]:
    payload = _server.call("verify_audit", {"from_size": from_size, "to_size": to_size})
    if not payload.get("ok") and "no audit log" in str(payload.get("error", "")):
        raise HTTPException(status_code=404, detail=payload["error"])
    return payload


# --------------------------------------------------------------------------- #
# MCP passthrough
# --------------------------------------------------------------------------- #
@app.get("/mcp/tools", tags=["mcp"])
def mcp_tools() -> dict[str, Any]:
    return {"tools": TOOLS, "boundary": mcp_boundary(), "snippet": adoption_snippet()}


@app.post("/mcp/call/{tool}", tags=["mcp"])
def mcp_call(tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = _server.call(tool, arguments or {})
    if not payload.get("ok") and payload.get("error", "").startswith("no such tool"):
        raise HTTPException(status_code=404, detail=payload["error"])
    return payload


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _today() -> dt.date:
    return dt.date(2026, 3, 31)
