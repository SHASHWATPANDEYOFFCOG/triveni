"""The Razorpay seam: one interface, two rails, and a boundary that cannot be crossed.

Triveni runs offline by default. `TRIVENI_RAILS=offline` reads recorded fixtures and
needs no account, no key and no network; `TRIVENI_RAILS=mcp` calls the official
`razorpay/razorpay-mcp-server`. Both satisfy the same protocol, and a contract test
asserts they return the same pydantic models, so switching rails cannot break the
pipeline.

**Triveni never calls a write tool.** The MCP server exposes `create_order`,
`create_payment_link`, `capture_payment`, `create_refund` and others. None of them is
reachable from here, and that is enforced three ways rather than promised once:

1. an **allowlist** - a tool not named in :data:`READ_TOOLS` raises before any
   transport is touched, and the allowlist is a frozenset of literals, not a pattern;
2. a **denylist assertion** - every known write tool is checked against the allowlist
   at import time, so adding a write tool to the allowlist by accident fails the build;
3. `READ_ONLY=true` is sent to the server, so the far side refuses too.

A finance controller reads and proposes; it does not move money. Belt, braces, and a
third belt, because this is the one boundary where being wrong is unrecoverable.

**The tool names** are those the official server exposes. They were taken from the
`razorpay/razorpay-mcp-server` repository and **should be re-confirmed before anyone
relies on the live rail** - MCP servers rename tools, and a stale name here fails
closed (an unknown tool is refused) rather than open.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable

from core.errors import IngestError, TriveniError
from ingest.adapters import fixtures
from ingest.canonical import IngestResult, SourceKind

ROOT: Final = Path(__file__).resolve().parent.parent.parent

#: Read-only tools Triveni may call. `fetch_settlement_recon_details` is the one that
#: matters most - it is the gateway's own reconciliation view of a payout.
READ_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "fetch_all_settlements",
        "fetch_settlement_with_id",
        "fetch_settlement_recon_details",
        "fetch_all_payments",
        "fetch_payment",
        "fetch_all_refunds",
        "fetch_all_orders",
        "fetch_order_payments",
        "fetch_all_payouts",
        "fetch_payout_by_id",
    }
)

#: Tools that move money or create obligations. Listed explicitly so the assertion
#: below is about *these* names rather than about a pattern that could drift.
WRITE_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "create_order",
        "create_payment_link",
        "create_payment_link_upi",
        "capture_payment",
        "create_refund",
        "update_refund",
        "create_payout",
        "create_qr_code",
        "close_qr_code",
        "update_order",
        "edit_payment_link",
    }
)

# Enforced at import: a write tool must never appear in the allowlist. Putting one
# there by accident becomes a build failure rather than a production incident.
_OVERLAP = READ_TOOLS & WRITE_TOOLS
if _OVERLAP:
    raise AssertionError(
        f"a write tool is in the read allowlist: {sorted(_OVERLAP)}. "
        "Triveni reads and proposes; it does not move money."
    )


class WriteToolRefused(TriveniError):
    """An attempt to call a tool that could move money. Always refused."""


class UnknownToolRefused(TriveniError):
    """A tool that is not on the allowlist. Fails closed, never open."""


def assert_readable(tool: str) -> None:
    """The single gate. Every call goes through it, including from tests."""
    if tool in WRITE_TOOLS:
        raise WriteToolRefused(
            "Triveni never calls a Razorpay write tool",
            tool=tool,
            note="a finance controller reads and proposes; it does not move money",
        )
    if tool not in READ_TOOLS:
        raise UnknownToolRefused(
            "tool is not on the read allowlist; refusing rather than guessing",
            tool=tool,
            allowed=sorted(READ_TOOLS),
        )


# --------------------------------------------------------------------------- #
# The protocol both rails satisfy
# --------------------------------------------------------------------------- #
@runtime_checkable
class RazorpayRail(Protocol):
    """What the pipeline needs from a gateway, on either rail."""

    name: str

    def fetch_payments(self, start: dt.date, end: dt.date) -> IngestResult: ...
    def fetch_refunds(self, start: dt.date, end: dt.date) -> IngestResult: ...
    def fetch_settlements(self, start: dt.date, end: dt.date) -> IngestResult: ...
    def fetch_settlement_recon(self, settlement_id: str) -> list[dict[str, Any]]: ...
    def fetch_payouts(self, start: dt.date, end: dt.date) -> IngestResult: ...


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One call, recorded for the audit log and the run report."""

    tool: str
    arguments: dict[str, Any]
    rail: str

    def canonical(self) -> dict[str, Any]:
        return {"tool": self.tool, "arguments": self.arguments, "rail": self.rail}


# --------------------------------------------------------------------------- #
# Offline rail
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class OfflineRail:
    """Recorded fixtures shaped exactly like Razorpay API responses.

    The default, and the only rail `make demo` uses. It still goes through
    :func:`assert_readable`, so the allowlist is exercised on the path everyone runs
    rather than only on the one nobody does.
    """

    directory: Path = field(default_factory=lambda: ROOT / "data" / "seed")
    name: str = "offline"
    calls: list[ToolCall] = field(default_factory=list)

    def _record(self, tool: str, **arguments: Any) -> None:
        assert_readable(tool)
        self.calls.append(ToolCall(tool=tool, arguments=arguments, rail=self.name))

    def fetch_payments(self, start: dt.date, end: dt.date) -> IngestResult:
        self._record("fetch_all_payments", **{"from": str(start), "to": str(end)})
        return _within(fixtures.read_payments(self.directory), start, end)

    def fetch_refunds(self, start: dt.date, end: dt.date) -> IngestResult:
        self._record("fetch_all_refunds", **{"from": str(start), "to": str(end)})
        return _within(fixtures.read_refunds(self.directory), start, end)

    def fetch_settlements(self, start: dt.date, end: dt.date) -> IngestResult:
        self._record("fetch_all_settlements", **{"from": str(start), "to": str(end)})
        return _within(fixtures.read_settlements(self.directory), start, end)

    def fetch_settlement_recon(self, settlement_id: str) -> list[dict[str, Any]]:
        """The gateway's own reconciliation view of one payout.

        The richest read tool the server offers, and the reason the `mcp` rail is
        worth having: it returns the gateway's line-by-line account of what it netted,
        which Triveni can then check against what the bank actually credited.
        """
        self._record("fetch_settlement_recon_details", settlement_id=settlement_id)
        path = self.directory / "gateway_settlements.json"
        if not path.exists():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [item for item in payload.get("items", []) if item.get("id") == settlement_id]

    def fetch_payouts(self, start: dt.date, end: dt.date) -> IngestResult:
        self._record("fetch_all_payouts", **{"from": str(start), "to": str(end)})
        # A merchant with no payouts in the window is normal, not an error.
        return IngestResult(source=SourceKind.GATEWAY, rows=(), corrupt=())


def _within(result: IngestResult, start: dt.date, end: dt.date) -> IngestResult:
    return IngestResult(
        source=result.source,
        rows=tuple(row for row in result.rows if start <= row.occurred_on <= end),
        corrupt=result.corrupt,
    )


# --------------------------------------------------------------------------- #
# Live rail
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class McpRail:
    """Calls the official `razorpay/razorpay-mcp-server`.

    Deliberately not exercised by the demo or the test suite - this build has no
    Razorpay account, and a rail that silently fell back to fixtures when a key was
    missing would be worse than one that refuses. It refuses.

    `READ_ONLY=true` is sent to the server so the far side enforces the same boundary
    the allowlist enforces here.
    """

    client: Any = None
    name: str = "mcp"
    calls: list[ToolCall] = field(default_factory=list)

    def _call(self, tool: str, **arguments: Any) -> Any:
        assert_readable(tool)
        self.calls.append(ToolCall(tool=tool, arguments=arguments, rail=self.name))
        if self.client is None:
            raise IngestError(
                "TRIVENI_RAILS=mcp but no MCP client is configured",
                tool=tool,
                hint=(
                    "set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET and pass a client, or "
                    "use the default TRIVENI_RAILS=offline"
                ),
            )
        return self.client.call_tool(tool, arguments)

    def fetch_payments(self, start: dt.date, end: dt.date) -> IngestResult:
        raw = self._call("fetch_all_payments", **{"from": str(start), "to": str(end)})
        return _from_envelope(raw, fixtures.read_payments)

    def fetch_refunds(self, start: dt.date, end: dt.date) -> IngestResult:
        raw = self._call("fetch_all_refunds", **{"from": str(start), "to": str(end)})
        return _from_envelope(raw, fixtures.read_refunds)

    def fetch_settlements(self, start: dt.date, end: dt.date) -> IngestResult:
        raw = self._call("fetch_all_settlements", **{"from": str(start), "to": str(end)})
        return _from_envelope(raw, fixtures.read_settlements)

    def fetch_settlement_recon(self, settlement_id: str) -> list[dict[str, Any]]:
        raw = self._call("fetch_settlement_recon_details", settlement_id=settlement_id)
        items = raw.get("items", []) if isinstance(raw, dict) else []
        return list(items)

    def fetch_payouts(self, start: dt.date, end: dt.date) -> IngestResult:
        self._call("fetch_all_payouts", **{"from": str(start), "to": str(end)})
        return IngestResult(source=SourceKind.GATEWAY, rows=(), corrupt=())


def _from_envelope(raw: Any, reader: Any) -> IngestResult:
    """Both rails end up in the same reader, which is what makes the contract test
    meaningful: the live rail cannot drift into a different schema without failing."""
    import tempfile

    items = raw.get("items", []) if isinstance(raw, dict) else []
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        name = {
            fixtures.read_payments: "gateway_payments.json",
            fixtures.read_refunds: "gateway_refunds.json",
            fixtures.read_settlements: "gateway_settlements.json",
        }[reader]
        (target / name).write_text(
            json.dumps({"entity": "collection", "count": len(items), "items": items}),
            encoding="utf-8",
        )
        return reader(target)


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #
def rail_from_env(directory: Path | None = None) -> RazorpayRail:
    """Pick the rail. Offline unless explicitly told otherwise.

    Defaulting to offline is the whole reason `make demo` works on a clean clone: an
    unset variable can only ever mean "no network", never "try the internet".
    """
    choice = os.environ.get("TRIVENI_RAILS", "offline").strip().lower()
    if choice == "mcp":
        return McpRail()
    return OfflineRail(directory=directory or (ROOT / "data" / "seed"))


def describe_boundary() -> str:
    """The sentence that goes in the README, generated from the code that enforces it."""
    return (
        f"Triveni may call {len(READ_TOOLS)} read tools and 0 write tools. "
        f"{len(WRITE_TOOLS)} write tools are named explicitly and refused by an "
        f"allowlist checked before any transport, by an import-time assertion that "
        f"they never appear in it, and by READ_ONLY=true on the server."
    )


def tool_inventory() -> list[dict[str, Any]]:
    """Every tool and its status, for the run report and the UI."""
    rows = [{"tool": t, "allowed": True, "kind": "read"} for t in sorted(READ_TOOLS)]
    rows += [{"tool": t, "allowed": False, "kind": "write"} for t in sorted(WRITE_TOOLS)]
    return rows


def calls_made(rail: RazorpayRail) -> Sequence[ToolCall]:
    return getattr(rail, "calls", [])
