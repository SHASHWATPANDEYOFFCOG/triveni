#!/usr/bin/env python
"""Triveni task runner - the single source of truth for every build target.

The Makefile and make.cmd both delegate here so that a target behaves identically
on Linux, macOS and Windows (where `make` usually does not exist at all).

    python tasks.py <target> [args...]

Determinism: this script re-executes itself with PYTHONHASHSEED=0 so that every
target - tests, eval, demo - runs under an identical hashing regime. See
docs/adr/0002-task-runner.md.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
SEED = "0"

# --------------------------------------------------------------------------- #
# Determinism guard: re-exec once with a fixed hash seed.
# --------------------------------------------------------------------------- #
if os.environ.get("PYTHONHASHSEED") != SEED and not os.environ.get("_TRIVENI_REEXEC"):
    env = dict(os.environ, PYTHONHASHSEED=SEED, _TRIVENI_REEXEC="1")
    raise SystemExit(subprocess.call([sys.executable, __file__, *sys.argv[1:]], env=env))


def venv_python() -> str:
    """Path to the interpreter inside .venv, or the current one if there is none."""
    for rel in ("Scripts/python.exe", "bin/python"):
        cand = VENV / rel
        if cand.exists():
            return str(cand)
    return sys.executable


def run(cmd: Sequence[str], *, check: bool = True, env: dict[str, str] | None = None) -> int:
    printable = " ".join(str(c) for c in cmd)
    print(f"\033[2m$ {printable}\033[0m", flush=True)
    full_env = dict(os.environ, PYTHONHASHSEED=SEED, PYTHONIOENCODING="utf-8")
    full_env.pop("_TRIVENI_REEXEC", None)
    if env:
        full_env.update(env)
    code = subprocess.call(list(cmd), cwd=ROOT, env=full_env)
    if check and code != 0:
        raise SystemExit(code)
    return code


def py(*args: str, check: bool = True) -> int:
    return run([venv_python(), *args], check=check)


def banner(text: str) -> None:
    print(f"\n\033[1;33m== {text} ==\033[0m", flush=True)


# --------------------------------------------------------------------------- #
# Targets
# --------------------------------------------------------------------------- #
TARGETS: dict[str, Callable[[list[str]], None]] = {}


def target(name: str, help_: str) -> Callable[[Callable[[list[str]], None]], Callable[[list[str]], None]]:
    def deco(fn: Callable[[list[str]], None]) -> Callable[[list[str]], None]:
        fn.__doc__ = help_
        TARGETS[name] = fn
        return fn

    return deco


@target("setup", "Create .venv and install pinned dependencies (no credentials needed)")
def t_setup(_: list[str]) -> None:
    banner("setup")
    if not VENV.exists():
        run([sys.executable, "-m", "venv", str(VENV)])
    py("-m", "pip", "install", "--upgrade", "pip", "--quiet")
    py("-m", "pip", "install", "--quiet", "-e", ".[dev]")
    env_file = ROOT / ".env"
    if not env_file.exists():
        shutil.copyfile(ROOT / ".env.example", env_file)
        print("wrote .env from .env.example (all values blank - Triveni runs offline by default)")
    print("\n\033[1;32msetup complete\033[0m - run `make demo` (no API keys required)")


@target("run", "Start the FastAPI server on :8000")
def t_run(args: list[str]) -> None:
    banner("run")
    port = args[0] if args else os.environ.get("TRIVENI_PORT", "8000")
    py("-m", "uvicorn", "api.main:app", "--host", "127.0.0.1", "--port", port)


@target("test", "Run the full test suite (unit + property + golden + e2e)")
def t_test(args: list[str]) -> None:
    banner("test")
    py("-m", "pytest", *(args or []))


@target("lint", "Ruff lint + format check")
def t_lint(_: list[str]) -> None:
    banner("lint")
    py("-m", "ruff", "check", ".", check=False)


@target("typecheck", "mypy --strict on core/ and recon/")
def t_typecheck(_: list[str]) -> None:
    banner("typecheck")
    py("-m", "mypy", check=False)


@target("eval", "Run the evaluation harness -> metrics.json (byte-identical across runs)")
def t_eval(args: list[str]) -> None:
    banner("eval")
    py("-m", "scripts.eval_run", *args)


@target("demo", "The full offline narrative demo on committed seed data (<90s, no keys)")
def t_demo(args: list[str]) -> None:
    banner("demo")
    py("-m", "scripts.demo", *args)


@target("bench", "Throughput benchmark: rows/sec through every pipeline stage")
def t_bench(args: list[str]) -> None:
    banner("bench")
    py("-m", "scripts.bench", *args)


@target("verify", "Verify the Merkle audit log: re-derive the root, locate any tampering")
def t_verify(args: list[str]) -> None:
    banner("verify")
    py("-m", "core.audit.verify", *args)


@target("redteam", "Adversarial suite: prompt injection, tampering, ambiguity, corrupt rows")
def t_redteam(args: list[str]) -> None:
    banner("redteam")
    py("-m", "scripts.redteam", *args)


@target("data", "Regenerate the synthetic 3-source dataset with ground-truth labels")
def t_data(args: list[str]) -> None:
    banner("data")
    py("-m", "data.gen", *args)


@target("web", "Run the Next.js dashboard in dev mode on :3000")
def t_web(_: list[str]) -> None:
    banner("web")
    web = ROOT / "web"
    if not (web / "node_modules").exists():
        run(["npm", "install"], env={"NPM_CONFIG_FUND": "false"})
    run(["npm", "run", "dev"])


@target("secretscan", "Fail if anything that looks like a credential is committed")
def t_secretscan(_: list[str]) -> None:
    banner("secretscan")
    py("-m", "scripts.secretscan")


@target("clean", "Remove caches and generated artefacts (keeps .venv and committed seed data)")
def t_clean(_: list[str]) -> None:
    banner("clean")
    for pat in ("**/__pycache__", "**/.pytest_cache", "**/.mypy_cache", "**/.ruff_cache"):
        for p in ROOT.glob(pat):
            if p.is_dir() and ".venv" not in p.parts:
                shutil.rmtree(p, ignore_errors=True)
    for f in ("metrics.json", "audit.db"):
        (ROOT / f).unlink(missing_ok=True)
    print("clean")


@target("all", "setup -> lint -> typecheck -> test -> eval -> demo (the whole gate)")
def t_all(_: list[str]) -> None:
    for name in ("setup", "lint", "typecheck", "test", "eval", "demo"):
        TARGETS[name]([])


@target("help", "List every target")
def t_help(_: list[str]) -> None:
    print("\n\033[1mTriveni targets\033[0m  (use `make <target>` or `python tasks.py <target>`)\n")
    for name, fn in TARGETS.items():
        print(f"  \033[36m{name:12}\033[0m {fn.__doc__}")
    print()


def main() -> None:
    argv = sys.argv[1:]
    name = argv[0] if argv else "help"
    fn = TARGETS.get(name)
    if fn is None:
        print(f"\033[31munknown target: {name}\033[0m")
        TARGETS["help"]([])
        raise SystemExit(2)
    fn(argv[1:])


if __name__ == "__main__":
    main()
