# Triveni — build progress

Human-readable mirror of `.triveni/state.json`. Rewritten every loop iteration.

**Current milestone:** COMPLETE — M0 through M19 all green.
**Status:** 837 tests · mypy --strict clean · ruff clean · money-lint clean · redteam 13/13 · QA 12+4/16
**Blocked:** none

## Environment resolved (2026-09-02)
Python 3.14.3 · Node 24.14 · git 2.50 · **no `make`, no `uv`, no `docker`** on this machine.
All pinned dependencies verified installable with cp314 wheels and their APIs executed
before being written into `pyproject.toml` — including `ortools` (CP-SAT), `scipy`
(`linear_sum_assignment`, `nnls`), `cryptography` (Ed25519) and `statsmodels`. No
algorithmic fallbacks were needed. See ADR 0003.

## Green milestones
- **M0** — repo, tooling, task runner, Docker, `.env.example`, ADRs 0001-0003.
  DoD: `python tasks.py test` exits 0 on a clean tree; the server boots with every
  credential env var scrubbed and `/health` returns `200 {"credentials_required": false}`.
- **M1** — `core/money`, `core/clock`, `core/ids` + property tests.
  DoD: **104 tests green, `mypy --strict` clean on `core/`+`recon/`, `ruff` clean.**
  The no-float proof runs in both directions: `scripts/money_lint.py` walks the AST of
  every money-path module and rejects float literals, `float()` calls and non-Decimal
  division (with a reason-mandatory escape hatch), and a test feeds the lint a
  deliberately broken module to prove it is not vacuous; Hypothesis then generates
  arbitrary floats against every constructor and operator.

## Notes
- `tasks.py` is the single source of truth for build targets; `Makefile` and `make.cmd`
  both delegate to it, so `make demo` works on Windows too (ADR 0002).
- Docker files are written but **unverified** (no Docker on this machine) — recorded in
  `docs/limitations.md` rather than claimed as working.

## Next
M2 — RFC-6962-style Merkle transparency log: inclusion proofs, consistency proofs,
Ed25519-signed tree head, and `make verify` locating the exact tampered index.
