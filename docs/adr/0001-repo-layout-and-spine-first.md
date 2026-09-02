# ADR 0001 - Repository layout and the spine-first rule

**Status:** accepted - 2026-09-02

**Context.** Triveni is one product with three loops (Close, Explain, Foresee). The
failure mode for a build this size is feature logic leaking into shared modules, so
that nothing can be tested in isolation and the invariants have no single home.

**Decision.** `core/` holds the spine - money, clock, ids, audit, policy, conformal,
cost model, eval, llm gateway - and contains **no feature logic**. Loops live in
`recon/`, `forecast/`, `qa/`, and may import `core/` but never each other. The repo
root *is* the project root (no nested `triveni/` directory), so a judge who opens the
folder is already at the Makefile.

**Consequence.** No loop may be started before the spine's tests are green. `core/`
is the only package under `mypy --strict` together with `recon/`. A module that needs
to import across loops is a design smell and gets refactored into `core/` instead.
