# ADR 0002 - `tasks.py` is the single source of truth for build targets

**Status:** accepted - 2026-09-02

**Context.** The constitution requires `make setup|run|test|eval|demo|bench|verify`.
GNU `make` is not installed on this development machine (Windows 11) and will not be
present for many judges either. Duplicating target logic between a Makefile and a
`.ps1`/`.cmd` script guarantees the two drift apart.

**Decision.** All target logic lives in `tasks.py`. The `Makefile` is a one-line
delegator (`$(PY) tasks.py $@`) and `make.cmd` is a Windows shim that dispatches to
the same file, so `make demo` works verbatim in bash, PowerShell and cmd.

**Consequence.** One implementation, three entry points. `tasks.py` also re-execs
itself with `PYTHONHASHSEED=0`, which is how the determinism requirement is enforced
globally rather than being remembered per-target.
