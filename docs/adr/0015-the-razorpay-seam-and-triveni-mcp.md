# ADR 0015 - The Razorpay seam, and Triveni as an MCP server

**Status:** accepted - 2026-09-03

**Context.** Triveni must run with no Razorpay account and no network, and must
simultaneously demonstrate that it slots into Razorpay's rails. Those pull in opposite
directions, and the usual compromise - a live path that silently falls back to fixtures
when a key is missing - is the worst of both: it makes an offline run look like a live
one.

**Decision, part one: two rails, one protocol, and a boundary enforced three times.**
`TRIVENI_RAILS=offline` (the default) reads recorded fixtures; `mcp` calls the official
server. Both satisfy the same `RazorpayRail` protocol and land in the same reader, so a
contract test can assert the live rail cannot drift into a different schema. An unset
variable can only ever mean "no network"; the live rail **refuses** without a client
rather than falling back.

Triveni never calls a write tool, and that is enforced by:

1. an **allowlist of 10 literal tool names** - not a pattern, because `fetch_*` would
   silently admit a future `fetch_and_capture`. A test reads the source to assert no
   wildcard is in the declaration.
2. an **import-time assertion** that the 11 named write tools never appear in it, so
   adding one by accident is a build failure rather than a production incident.
3. `READ_ONLY=true` sent to the server, so the far side refuses too.

The offline rail goes through the same gate, so the check is exercised on the path
everyone runs rather than only on the one nobody does.

**Decision, part two: expose MCP as well as consume it.** Six tools -
`close_books`, `explain_settlement`, `list_exceptions`, `resolve_exception`,
`forecast_cash`, `verify_audit` - each returning structured JSON with a **mandatory
`reason` field**. An agent composing Triveni into a larger workflow has to explain what
it did to *its* user; `{"matched": 214}` makes that impossible, and the same number with
"214 of 231 claims matched; 17 routed to review because their confidence fell below the
conformal threshold of 0.9548" makes it trivial.

Exactly one tool mutates state. `resolve_exception` records a **human's** decision -
requiring both an approver and a reason, refusing without either - routes it through
the same guarded wrapper as everything else, and returns the Merkle inclusion proof so
the caller can verify the record exists **without trusting the response**. There is no
tool that moves money, so there is nothing to talk into moving money.

**`forecast_cash` reports its own absence.** M14 has not landed, and the tool returns
`ok: false` with an explanation rather than a fabricated series. Declaring the contract
early lets an agent discover it; inventing the data would be exactly what rule A.6
forbids.

**The close streams.** A reconciliation takes ten seconds on the seed and a minute at
scale. `/close/stream` emits one SSE event per stage, which is both a better product
than a spinner and a more honest one: a slow run becomes diagnosable rather than merely
slow. Plain `StreamingResponse`, no extra dependency, because the traffic is
one-directional.

**11 routes, 46 tests.** The most useful of them assert the boundary rather than the
happy path: every write tool refused by name, an unknown tool failing closed, no route
whose path contains `payout` or `transfer`, and the boundary description generated from
the same frozensets the allowlist checks against - so it cannot describe a boundary the
code does not enforce.
