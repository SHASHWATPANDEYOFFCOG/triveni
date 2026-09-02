"""Fail the build if anything credential-shaped is about to be committed.

Runs over tracked files only (or the whole tree when git is unavailable) and is
wired into `make secretscan`, the test suite and the pre-commit hook. The rule in
PART A.5 is absolute: no secrets in the repo or in git history, ever.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Each pattern is (name, regex). Deliberately tuned to catch real key shapes
# rather than the word "key", so that documentation stays writable.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("razorpay_live_key", re.compile(r"rzp_live_[A-Za-z0-9]{10,}")),
    ("razorpay_test_key", re.compile(r"rzp_test_[A-Za-z0-9]{10,}")),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("slack_token", re.compile(r"xox[baprs]-[0-9A-Za-z\-]{10,}")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("generic_assignment", re.compile(r"(?i)\b(?:api[_-]?key|secret|password|passwd|token)\s*[:=]\s*['\"][^'\"\s]{16,}['\"]")),
]

SKIP_DIRS = {".venv", "node_modules", ".git", "__pycache__", ".next", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".qodo"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".pdf", ".mp4", ".db"}
# This file necessarily contains the patterns it hunts for.
SELF = Path(__file__).name


def candidate_files() -> list[Path]:
    try:
        out = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL)
        files = [ROOT / line for line in out.splitlines() if line.strip()]
        if files:
            return files
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        pass
    return [p for p in ROOT.rglob("*") if p.is_file()]


def scan() -> list[tuple[Path, int, str]]:
    findings: list[tuple[Path, int, str]] = []
    for path in sorted(candidate_files()):
        if not path.is_file():
            continue
        if SKIP_DIRS & set(path.parts) or path.suffix.lower() in SKIP_SUFFIXES:
            continue
        if path.name == SELF:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for name, pattern in PATTERNS:
                if pattern.search(line):
                    findings.append((path.relative_to(ROOT), lineno, name))
    return findings


def main() -> int:
    findings = scan()
    if findings:
        print("\033[31mSECRET SCAN FAILED\033[0m")
        for path, lineno, name in findings:
            print(f"  {path}:{lineno}  {name}")
        return 1
    print(f"\033[32msecret scan clean\033[0m ({len(candidate_files())} files, {len(PATTERNS)} patterns)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
