"""``make verify`` / ``python -m core.audit.verify`` - the independent auditor.

This deliberately re-derives everything from the stored bytes rather than trusting
any in-memory state, and it is written so that a judge can run it against a log
Triveni produced and get a yes/no answer plus, when the answer is no, the exact index
at which the log stopped telling the truth.

    python -m core.audit.verify                       # verify the default log
    python -m core.audit.verify --db path/to.db       # verify a specific log
    python -m core.audit.verify --prove 42            # inclusion proof for record 42
    python -m core.audit.verify --consistency 100 400 # append-only proof between sizes
    python -m core.audit.verify --tamper 4            # break it on purpose, then report
    python -m core.audit.verify --json                # machine-readable output
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.audit.log import DEFAULT_DB, AuditLog

_GREEN = "\033[32m"
_RED = "\033[31m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def _colourise(report_text: str) -> str:
    out = []
    for line in report_text.splitlines():
        if "[PASS]" in line:
            out.append(line.replace("[PASS]", f"{_GREEN}[PASS]{_RESET}"))
        elif "[FAIL]" in line or "TAMPERING" in line:
            out.append(f"{_RED}{line}{_RESET}")
        elif line.strip() in {"VERIFIED", "VERIFICATION FAILED"}:
            colour = _GREEN if line.strip() == "VERIFIED" else _RED
            out.append(f"{colour}{_BOLD}{line}{_RESET}")
        else:
            out.append(line)
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="triveni verify",
        description="Re-derive the Merkle root of Triveni's decision log and locate any tampering.",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="path to the audit database")
    parser.add_argument("--prove", type=int, metavar="SEQ", help="emit an inclusion proof for one record")
    parser.add_argument(
        "--consistency",
        type=int,
        nargs=2,
        metavar=("FROM", "TO"),
        help="emit an append-only consistency proof between two tree sizes",
    )
    parser.add_argument(
        "--tamper",
        type=int,
        metavar="SEQ",
        help="ADVERSARY SIMULATION: rewrite a committed record, then verify (demo only)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.db.exists():
        print(f"{_RED}no audit log at {args.db}{_RESET}")
        print(f"{_DIM}run `make demo` first - it closes a day's books and writes the log{_RESET}")
        return 2

    with AuditLog(args.db) as log:
        if args.tamper is not None:
            original = log.record_at(args.tamper)
            print(
                f"{_DIM}adversary simulation: rewriting the reason on record "
                f"{args.tamper} directly in the database, bypassing the "
                f"append-only triggers{_RESET}\n"
            )
            log.tamper_for_demo(args.tamper, "approved by finance head")
            print(f"{_DIM}  was: {original.reason!r}{_RESET}")
            print(f"{_DIM}  now: 'approved by finance head'{_RESET}\n")

        if args.prove is not None:
            proof = log.inclusion_proof(args.prove)
            head = log.latest_head()
            ok = proof.verify(head.root)
            payload = {
                "kind": "inclusion",
                "record": log.record_at(args.prove).canonical(),
                "proof": proof.to_json(),
                "root": head.root.hex(),
                "verified": ok,
            }
            if args.json:
                print(json.dumps(payload, indent=2, default=str))
            else:
                print(f"inclusion proof for record {args.prove}")
                print(f"  tree size : {proof.tree_size}")
                print(f"  path      : {len(proof.path)} hashes (log2 of the log)")
                for i, node in enumerate(proof.path):
                    print(f"    {i}: {node.hex()}")
                print(f"  root      : {head.root.hex()}")
                mark = f"{_GREEN}VERIFIED{_RESET}" if ok else f"{_RED}FAILED{_RESET}"
                print(f"  result    : {mark}")
            return 0 if ok else 1

        if args.consistency is not None:
            first, second = args.consistency
            consistency = log.consistency_proof(first, second)
            ok = consistency.verify(log.root(first), log.root(second))
            if args.json:
                print(json.dumps({"kind": "consistency", **consistency.to_json(), "verified": ok}, indent=2))
            else:
                print(f"append-only proof: size {first} -> size {second}")
                print(f"  path   : {len(consistency.path)} hashes")
                for i, node in enumerate(consistency.path):
                    print(f"    {i}: {node.hex()}")
                mark = f"{_GREEN}VERIFIED{_RESET}" if ok else f"{_RED}FAILED{_RESET}"
                print(f"  result : {mark}")
                if ok:
                    print(
                        f"{_DIM}  i.e. every record in the size-{first} log is still "
                        f"present, unmodified, in the size-{second} log{_RESET}"
                    )
            return 0 if ok else 1

        report = log.verify()
        head = log.latest_head()
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": report.ok,
                        "size": report.size,
                        "root": report.root.hex(),
                        "head": head.to_json(),
                        "checks": [
                            {"name": n, "passed": p, "detail": d} for n, p, d in report.checks
                        ],
                        "tampered_index": report.tampered_index,
                    },
                    indent=2,
                )
            )
        else:
            print(_colourise(report.render()))
            if report.ok:
                print(
                    f"\n{_DIM}This is the RFC 6962 construction that secures the web PKI,"
                    f"\napplied to a financial decision log. Try: "
                    f"python -m core.audit.verify --tamper 4{_RESET}"
                )
        return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
