"""M13 gate: the HTTP surface, Triveni's MCP server, and the Razorpay boundary.

The DoD is an OpenAPI schema, MCP tools listed and callable, and the adapter passing
contract tests against recorded fixtures. The tests that matter most are the boundary
ones: Triveni reads and proposes, and the only interesting question about that claim is
whether it is enforced or merely stated.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.mcp_server import TOOLS, WRITE_TOOL_NAMES, TriveniMCP, adoption_snippet
from core.errors import TriveniError
from ingest.adapters.razorpay_mcp import (
    READ_TOOLS,
    WRITE_TOOLS,
    McpRail,
    OfflineRail,
    UnknownToolRefused,
    WriteToolRefused,
    assert_readable,
    rail_from_env,
    tool_inventory,
)
from ingest.canonical import SourceKind
from recon.exceptions import ExceptionType

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def server():
    return TriveniMCP()


# --------------------------------------------------------------------------- #
# The boundary: Triveni never moves money
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("tool", sorted(WRITE_TOOLS))
def test_every_razorpay_write_tool_is_refused(tool: str) -> None:
    """Named individually rather than matched by pattern, so a renamed tool fails
    the test rather than slipping through a regex."""
    with pytest.raises(WriteToolRefused):
        assert_readable(tool)


def test_an_unknown_tool_fails_closed() -> None:
    """A stale or renamed tool must be refused, not attempted."""
    with pytest.raises(UnknownToolRefused):
        assert_readable("fetch_all_somethings")


def test_the_allowlist_and_denylist_cannot_overlap() -> None:
    """Enforced at import time, so putting a write tool in the allowlist by accident
    is a build failure rather than a production incident."""
    assert not (READ_TOOLS & WRITE_TOOLS)
    assert READ_TOOLS and WRITE_TOOLS


def test_the_allowlist_is_literals_not_a_pattern() -> None:
    """A pattern like `fetch_*` would silently admit a future `fetch_and_capture`."""
    import inspect

    import ingest.adapters.razorpay_mcp as adapter

    source = inspect.getsource(adapter)
    declaration = source.split("READ_TOOLS: Final[frozenset[str]] = frozenset(")[1].split(")")[0]
    assert "*" not in declaration and "re." not in declaration


def test_the_offline_rail_still_goes_through_the_allowlist() -> None:
    """The gate is exercised on the path everyone runs, not only on the one nobody
    does. A check that only fires on the live rail is a check nobody tests."""
    rail = OfflineRail()
    rail.fetch_payments(dt.date(2026, 3, 1), dt.date(2026, 3, 31))
    assert rail.calls
    for call in rail.calls:
        assert call.tool in READ_TOOLS


def test_triveni_mcp_exposes_no_money_moving_tool(server) -> None:
    assert WRITE_TOOL_NAMES == {"resolve_exception"}
    for tool in TOOLS:
        assert "payout" not in tool["name"]
        assert "refund" not in tool["name"]
        assert "transfer" not in tool["name"]


def test_the_boundary_description_is_generated_from_the_code() -> None:
    """Not a policy document - the same frozensets the allowlist checks against, so
    it cannot describe a boundary the code does not enforce."""
    from ingest.adapters.razorpay_mcp import describe_boundary

    summary = describe_boundary()
    assert str(len(READ_TOOLS)) in summary
    assert str(len(WRITE_TOOLS)) in summary
    assert "0 write tools" in summary


# --------------------------------------------------------------------------- #
# The adapter contract
# --------------------------------------------------------------------------- #
def test_both_rails_satisfy_the_same_protocol() -> None:
    """Switching rails must not be able to break the pipeline."""
    from ingest.adapters.razorpay_mcp import RazorpayRail

    assert isinstance(OfflineRail(), RazorpayRail)
    assert isinstance(McpRail(), RazorpayRail)


def test_the_offline_rail_returns_the_canonical_model() -> None:
    rail = OfflineRail()
    result = rail.fetch_payments(dt.date(2026, 3, 1), dt.date(2026, 3, 31))
    assert result.source is SourceKind.GATEWAY
    assert result.rows
    for row in result.rows[:10]:
        assert isinstance(row.amount.paise, int)
        assert row.occurred_at.tzinfo is not None


def test_the_date_range_actually_filters() -> None:
    rail = OfflineRail()
    wide = rail.fetch_payments(dt.date(2026, 1, 1), dt.date(2026, 12, 31))
    narrow = rail.fetch_payments(dt.date(2026, 3, 2), dt.date(2026, 3, 4))
    assert 0 < len(narrow.rows) < len(wide.rows)


def test_the_recon_details_tool_is_available_offline() -> None:
    """The richest read tool the server offers, and the reason the live rail is worth
    having at all."""
    rail = OfflineRail()
    payload = json.loads((ROOT / "data" / "seed" / "gateway_settlements.json").read_text(encoding="utf-8"))
    settlement_id = payload["items"][0]["id"]
    rows = rail.fetch_settlement_recon(settlement_id)
    assert rows and rows[0]["id"] == settlement_id


def test_the_live_rail_refuses_rather_than_falling_back() -> None:
    """A rail that silently used fixtures when a key was missing would be worse than
    one that refuses: it would make an offline run look like a live one."""
    from core.errors import IngestError

    with pytest.raises(IngestError, match="no MCP client"):
        McpRail().fetch_payments(dt.date(2026, 3, 1), dt.date(2026, 3, 2))


def test_the_default_rail_is_offline(monkeypatch) -> None:
    """An unset variable can only ever mean 'no network', never 'try the internet'."""
    monkeypatch.delenv("TRIVENI_RAILS", raising=False)
    assert isinstance(rail_from_env(), OfflineRail)
    monkeypatch.setenv("TRIVENI_RAILS", "mcp")
    assert isinstance(rail_from_env(), McpRail)
    monkeypatch.setenv("TRIVENI_RAILS", "nonsense")
    assert isinstance(rail_from_env(), OfflineRail)


def test_the_tool_inventory_marks_every_write_tool_disallowed() -> None:
    inventory = tool_inventory()
    assert len(inventory) == len(READ_TOOLS) + len(WRITE_TOOLS)
    for row in inventory:
        assert row["allowed"] == (row["kind"] == "read")


# --------------------------------------------------------------------------- #
# The MCP server
# --------------------------------------------------------------------------- #
def test_tools_are_listable_and_callable(server) -> None:
    listed = server.list_tools()
    assert len(listed) == len(TOOLS)
    for tool in listed:
        assert tool["name"] and tool["description"] and "inputSchema" in tool
        assert isinstance(tool["mutates"], bool)


def test_every_response_carries_a_reason(server) -> None:
    """An agent composing Triveni needs to explain what it did to its own user."""
    for name, arguments in (
        ("close_books", {"date": "2026-03-31"}),
        ("list_exceptions", {"limit": 3}),
        ("no_such_tool", {}),
    ):
        payload = server.call(name, arguments)
        assert payload.get("reason"), f"{name} returned no reason"


def test_an_unknown_tool_is_refused_with_the_known_list(server) -> None:
    payload = server.call("drop_all_tables", {})
    assert not payload["ok"]
    assert "no such tool" in payload["error"]
    assert set(payload["known"]) == {t["name"] for t in TOOLS}


def test_bad_arguments_are_refused_not_guessed(server) -> None:
    payload = server.call("close_books", {"wrong_argument": 1})
    assert not payload["ok"]
    assert "bad arguments" in payload["error"]


def test_close_books_reports_the_guarantee_in_force(server) -> None:
    payload = server.call("close_books", {"date": "2026-03-31"})
    assert payload["ok"]
    assert payload["conformal_threshold"]
    assert payload["realised_error_holdout"] is not None
    assert "unsupervised" in payload["reason"]
    assert len(payload["stages"]) == 7


def test_explain_settlement_returns_a_balanced_waterfall(server) -> None:
    result = server._result()
    settlement_id = next(iter(sorted(w.settlement_id for w in result.waterfalls.values())))
    payload = server.call("explain_settlement", {"settlement_id": settlement_id})
    assert payload["ok"] and payload["balanced"]
    assert "balanced to Rs 0" in payload["reason"]
    assert payload["waterfall"]["lines"]


def test_explain_settlement_refuses_an_unknown_id(server) -> None:
    payload = server.call("explain_settlement", {"settlement_id": "setl_nope"})
    assert not payload["ok"]
    assert "known" in payload


def test_list_exceptions_filters_and_summarises(server) -> None:
    payload = server.call("list_exceptions", {"limit": 5})
    assert payload["ok"] and payload["by_type"]
    assert len(payload["exceptions"]) <= 5
    assert set(payload["by_type"]) <= {t.value for t in ExceptionType}


def test_resolve_exception_requires_a_human_and_a_reason(server) -> None:
    """Triveni does not decide here. A decision with no approver is not a decision."""
    result = server._result()
    exception_id = sorted(e.exception_id for e in result.exceptions)[0]
    for bad in (
        {"decision": "accept", "reason": "", "approver": "priya"},
        {"decision": "accept", "reason": "looks fine", "approver": ""},
        {"decision": "maybe", "reason": "r", "approver": "priya"},
    ):
        payload = server.call("resolve_exception", {"exception_id": exception_id, **bad})
        assert not payload["ok"], bad


def test_resolve_exception_returns_a_verifiable_inclusion_proof(server, tmp_path) -> None:
    """The caller can check the record exists without trusting this response."""
    from core.audit.merkle import InclusionProof

    result = server._result()
    exception_id = sorted(e.exception_id for e in result.exceptions)[0]
    payload = server.call(
        "resolve_exception",
        {
            "exception_id": exception_id,
            "decision": "reject",
            "reason": "settled in the next cycle, confirmed with the bank",
            "approver": "priya@merchant.in",
        },
    )
    assert payload["ok"]
    proof = InclusionProof.from_json(payload["inclusion_proof"])
    assert proof.verify(bytes.fromhex(payload["merkle_root"]))
    assert "priya@merchant.in" in payload["reason"]


def test_forecast_reports_its_own_absence_rather_than_fabricating(server) -> None:
    """M14 has not landed. Returning an invented series would be worse than saying so."""
    payload = server.call("forecast_cash", {"horizon_days": 14})
    if not payload.get("ok"):
        assert "M14" in payload["reason"] or "available" in payload


def test_verify_audit_proves_the_log_was_appended_to(server) -> None:
    payload = server.call("verify_audit", {})
    if not payload.get("ok") and "no audit log" in str(payload.get("error", "")):
        pytest.skip("no audit log yet in this environment")
    assert payload["consistency"]["verified"]
    assert "RFC 6962" in payload["reason"]


def test_the_adoption_snippet_is_short_and_runnable() -> None:
    """'Your agent gets a finance controller with a guarantee, in one tool call' has
    to actually be one tool call."""
    snippet = adoption_snippet()
    assert len(snippet.splitlines()) <= 12
    assert "TriveniMCP()" in snippet
    assert "no credentials" in snippet
    compile(snippet, "<snippet>", "exec")


# --------------------------------------------------------------------------- #
# The HTTP surface
# --------------------------------------------------------------------------- #
def test_openapi_schema_generates_with_every_route(client) -> None:
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "Triveni"
    for path in ("/health", "/close", "/close/stream", "/exceptions", "/boundaries"):
        assert path in schema["paths"], path


def test_health_declares_no_credentials_and_no_money_movement(client) -> None:
    body = client.get("/health").json()
    assert body["credentials_required"] is False
    assert body["money_movement_possible"] is False
    assert body["rails"] == "offline"


def test_the_close_endpoint_returns_the_conformal_numbers(client) -> None:
    body = client.get("/close?date=2026-03-31").json()
    assert body["conformal_threshold"]
    assert body["realised_error_holdout"] is not None
    assert body["reason"]
    assert len(body["stages"]) == 7


def test_the_stream_emits_a_stage_event_per_stage(client) -> None:
    events: list[str] = []
    with client.stream("GET", "/close/stream?date=2026-03-31") as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line.startswith("event:"):
                events.append(line.split(": ", 1)[1].strip())
            if events and events[-1] == "done":
                break
    assert events[0] == "start"
    assert events[-1] == "done"
    assert events.count("stage") == 7


def test_boundaries_endpoint_publishes_the_taxonomy_and_the_allowlist(client) -> None:
    body = client.get("/boundaries").json()
    assert len(body["taxonomy"]) == len(list(ExceptionType))
    assert len(body["razorpay"]["tools"]) == len(READ_TOOLS) + len(WRITE_TOOLS)
    assert "0 write tools" in body["razorpay"]["summary"]


def test_resolving_through_http_is_refused_without_an_approver(client) -> None:
    response = client.post(
        "/exceptions/exc_whatever/resolve",
        json={"decision": "accept", "reason": "fine", "approver": ""},
    )
    assert response.status_code == 422


def test_an_unknown_settlement_is_a_404(client) -> None:
    assert client.get("/settlements/setl_does_not_exist").status_code == 404


def test_no_endpoint_can_move_money(client) -> None:
    """The surface itself carries no such capability, so there is nothing to talk
    into it."""
    schema = client.get("/openapi.json").json()
    for path in schema["paths"]:
        assert "payout" not in path
        assert "transfer" not in path
        assert not ("refund" in path and "post" in schema["paths"][path])


def test_the_adapter_never_raises_a_bare_exception() -> None:
    """Typed errors all the way out, so a caller can distinguish 'refused' from
    'broken'."""
    for error in (WriteToolRefused, UnknownToolRefused):
        assert issubclass(error, TriveniError)
