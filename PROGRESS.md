# Triveni — build progress

Human-readable mirror of `.triveni/state.json`. Rewritten every loop iteration.

**Current milestone:** M0 — repo, tooling, CI, Makefile, Docker, `.env.example`, ADR 0001
**Status:** in progress
**Blocked:** none

## Environment resolved (2026-09-02)
Python 3.14.3 · Node 24.14 · git 2.50 · **no `make`, no `uv`, no `docker`** on this machine.
All pinned dependencies verified installable with cp314 wheels and their APIs executed
before being written into `pyproject.toml` — including `ortools` (CP-SAT), `scipy`
(`linear_sum_assignment`, `nnls`), `cryptography` (Ed25519) and `statsmodels`. No
algorithmic fallbacks were needed. See ADR 0003.

## Green milestones
_(none yet)_

## Notes
- `tasks.py` is the single source of truth for build targets; `Makefile` and `make.cmd`
  both delegate to it, so `make demo` works on Windows too (ADR 0002).
- Docker files are written but **unverified** (no Docker on this machine) — recorded in
  `docs/limitations.md` rather than claimed as working.

## Next
M1 — `core/money`, `core/clock`, `core/ids` + property tests proving no float ever
enters a money path.
