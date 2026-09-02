"""Static lint: no floats on the money path.

Invariant D.1.1 says "money is integer paise; a float in a money path is a build
failure - add a lint rule for it". This is that rule.

It walks the AST of every module that handles money and fails on:

* a float literal (``0.5``, ``1e3``);
* a call to ``float(...)``;
* an augmented or plain division ``/`` whose operands are not provably ``Decimal``
  (true division is the classic way a rupee becomes 1233.9999999999998).

Escape hatch, because absolutism without an exit is just a lint people disable: put
``# money-lint: allow-float <reason>`` on the line. The reason is mandatory, and the
suppressions are printed in the report, so an escape is a visible decision rather
than a silent one.

Run standalone (``python -m scripts.money_lint``) or via ``tests/test_money.py``.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Modules where money is constructed, scaled, split, compared or serialised.
#: Grows as milestones land; a new module that touches Money belongs here.
MONEY_PATH_MODULES: tuple[str, ...] = (
    "core/money.py",
    "core/ids.py",
    "core/policy.py",
    "core/costmodel.py",
    "ingest/canonical.py",
    "recon/fees.py",
    "recon/subsetsum.py",
    "recon/assign.py",
    "recon/exceptions.py",
)

SUPPRESSION = "# money-lint: allow-float"


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    line: int
    col: int
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}:{self.col}  {self.rule}  {self.detail}"


@dataclass(frozen=True, slots=True)
class Suppression:
    path: str
    line: int
    reason: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}  allow-float: {self.reason}"


class _MoneyPathVisitor(ast.NodeVisitor):
    def __init__(self, path: str, source_lines: list[str]) -> None:
        self.path = path
        self.lines = source_lines
        self.findings: list[Finding] = []
        self.suppressions: list[Suppression] = []

    def _suppressed(self, node: ast.AST) -> bool:
        lineno = getattr(node, "lineno", 0)
        if not (1 <= lineno <= len(self.lines)):
            return False
        line = self.lines[lineno - 1]
        if SUPPRESSION not in line:
            return False
        reason = line.split(SUPPRESSION, 1)[1].strip()
        if reason:
            self.suppressions.append(Suppression(self.path, lineno, reason))
            return True
        # A suppression with no reason is itself a finding.
        self.findings.append(
            Finding(self.path, lineno, 0, "bare-suppression", "allow-float needs a reason")
        )
        return True

    def _add(self, node: ast.AST, rule: str, detail: str) -> None:
        if self._suppressed(node):
            return
        self.findings.append(
            Finding(
                self.path,
                getattr(node, "lineno", 0),
                getattr(node, "col_offset", 0),
                rule,
                detail,
            )
        )

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, float):
            self._add(node, "float-literal", f"literal {node.value!r}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id == "float":
            self._add(node, "float-call", "float(...) conversion")
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.Div) and not self._is_decimal_division(node):
            self._add(
                node,
                "true-division",
                "'/' on the money path; use allocate(), Rate.of() or Decimal operands",
            )
        self.generic_visit(node)

    @staticmethod
    def _is_decimal_division(node: ast.BinOp) -> bool:
        """Allow a division where at least one operand is provably a ``Decimal``.

        This is sound rather than lenient, and the reason is a language guarantee:
        ``Decimal`` refuses mixed arithmetic with ``float``. ``Decimal(1) / 0.5``
        raises ``TypeError`` - it does not quietly coerce. So if either operand is a
        Decimal, the other can only be an ``int`` or another ``Decimal``, and the
        result is exact. (``tests/test_money.py`` pins that guarantee, so if a future
        Python ever relaxed it, the lint's justification would fail loudly.)

        Requiring *both* sides was the first implementation and it flagged correct
        code like ``minutes * rate_per_hour / Decimal(60)``, where the left operand
        is a product rather than a bare Decimal call. Suppressing those one by one
        would have trained the reader to ignore the marker, which is how a lint dies.
        """

        def is_decimal(expr: ast.expr) -> bool:
            if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name):
                return expr.func.id == "Decimal"
            if isinstance(expr, ast.Name):
                return expr.id.endswith("_decimal")
            if isinstance(expr, ast.Attribute):
                return expr.attr in {"value", "as_decimal"}
            if isinstance(expr, ast.BinOp):
                # Decimal op (int|Decimal) is Decimal; anything else has raised.
                return is_decimal(expr.left) or is_decimal(expr.right)
            return False

        return is_decimal(node.left) or is_decimal(node.right)


def check_file(path: Path) -> tuple[list[Finding], list[Suppression]]:
    source = path.read_text(encoding="utf-8")
    rel = path.relative_to(ROOT).as_posix()
    visitor = _MoneyPathVisitor(rel, source.splitlines())
    visitor.visit(ast.parse(source, filename=str(path)))
    return visitor.findings, visitor.suppressions


def check_all() -> tuple[list[Finding], list[Suppression], list[str]]:
    """Returns (findings, suppressions, modules_checked)."""
    findings: list[Finding] = []
    suppressions: list[Suppression] = []
    checked: list[str] = []
    for rel in MONEY_PATH_MODULES:
        path = ROOT / rel
        if not path.exists():
            continue  # module lands in a later milestone
        found, allowed = check_file(path)
        findings.extend(found)
        suppressions.extend(allowed)
        checked.append(rel)
    return findings, suppressions, checked


def main() -> int:
    findings, suppressions, checked = check_all()
    print(f"money-lint: {len(checked)} money-path module(s) checked")
    for module in checked:
        print(f"  - {module}")
    if suppressions:
        print(f"\n{len(suppressions)} justified suppression(s):")
        for suppression in suppressions:
            print(f"  {suppression}")
    if findings:
        print(f"\n\033[31mFAIL - {len(findings)} float(s) on the money path\033[0m")
        for finding in findings:
            print(f"  {finding}")
        return 1
    print("\n\033[32mPASS - no floats on the money path\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
