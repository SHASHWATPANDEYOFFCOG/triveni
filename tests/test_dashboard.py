"""The dashboard had no tests, and that is exactly how a bug like this ships.

An adversarial review found `paintOffline(state.error)` called in `forecast.js` and
defined nowhere — a `ReferenceError` that fires on precisely the code path the
surrounding thirty lines of comment exist to handle gracefully. Nothing caught it,
because `make test` collected 765 tests across 18 files and not one of them opened
anything under `web/`.

These tests are the answer to that. They are static rather than a browser harness,
deliberately: a headless browser is a heavy dependency for a project whose whole
front-end argument is that it has none, and the failures worth catching here — an
undefined call, a missing export, an invented number, a table that scrolls the page —
are all visible in the source.

The undefined-identifier check is the one that matters. It is the reason this file
exists.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
SCREENS_DIR = WEB / "js" / "screens"

SCREENS = sorted(SCREENS_DIR.glob("*.js"))
MODULES = sorted((WEB / "js").rglob("*.js"))

#: Names that are legitimately available without being declared in the file.
_GLOBALS = frozenset(
    {
        # language + standard library
        "Array", "Object", "String", "Number", "Boolean", "Math", "JSON", "Date",
        "Map", "Set", "WeakMap", "Promise", "Error", "RegExp", "Symbol", "BigInt",
        "parseInt", "parseFloat", "isNaN", "isFinite", "encodeURIComponent",
        "decodeURIComponent", "structuredClone", "queueMicrotask",
        # DOM + browser
        "window", "document", "console", "fetch", "setTimeout", "clearTimeout",
        "setInterval", "clearInterval", "requestAnimationFrame",
        "cancelAnimationFrame", "performance", "localStorage", "sessionStorage",
        "location", "history", "navigator", "EventSource", "AbortController",
        "IntersectionObserver", "ResizeObserver", "MutationObserver",
        "HTMLElement", "HTMLInputElement", "HTMLTextAreaElement", "HTMLCanvasElement",
        "Element", "Node", "Event", "CustomEvent", "URL", "URLSearchParams",
        "getComputedStyle", "matchMedia", "alert", "confirm", "prompt",
        # control-flow keywords the call-site regex can pick up
        "if", "for", "while", "switch", "catch", "return", "typeof", "instanceof",
        "await", "async", "function", "super", "this", "new", "delete", "void",
        "in", "of", "do", "else", "try", "finally", "throw", "yield", "case",
        "default", "var", "let", "const", "import",
    }
)

_DECLARED = re.compile(
    r"(?:^|\s)(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"
    r"|(?:^|\s)(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)"
    r"|(?:^|\s)class\s+([A-Za-z_$][\w$]*)",
)
_IMPORTED = re.compile(r"import\s*\{([^}]+)\}\s*from")
_CALLS = re.compile(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def code_only(source: str) -> str:
    """Strip comments and string/template literals before scanning for calls.

    Without this the scan is useless: prose inside a comment or a template literal
    ("the settlement (see below)") looks exactly like a call to `settlement(`, and a
    check that reports seven imaginary problems per file gets deleted within a day.
    Stripping first is what makes the one real finding visible.
    """
    out: list[str] = []
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        pair = source[index : index + 2]
        if pair == "/*":
            end = source.find("*/", index + 2)
            index = length if end == -1 else end + 2
        elif pair == "//":
            end = source.find(chr(10), index)
            index = length if end == -1 else end
        elif char in "\"'`":
            quote = char
            index += 1
            while index < length:
                if source[index] == "\\":
                    index += 2
                    continue
                if source[index] == quote:
                    index += 1
                    break
                # A ${...} inside a template literal IS code and must be kept.
                if quote == "`" and source[index : index + 2] == "${":
                    depth = 1
                    index += 2
                    start = index
                    while index < length and depth:
                        if source[index] == "{":
                            depth += 1
                        elif source[index] == "}":
                            depth -= 1
                        index += 1
                    # Padded, because appending the fragments bare welds the last
                    # identifier of one to the first of the next: `${groundingLine}`
                    # followed by `esc(` became a call to `groundingLineesc`.
                    out.append(" " + code_only(source[start : index - 1]) + " ")
                    continue
                index += 1
        else:
            out.append(char)
            index += 1
    return "".join(out)


def declared_names(source: str) -> set[str]:
    """Every identifier the file introduces: declarations, imports, params, catches."""
    names: set[str] = set()
    for match in _DECLARED.finditer(source):
        names.update(name for name in match.groups() if name)
    for match in _IMPORTED.finditer(source):
        for piece in match.group(1).split(","):
            names.add(piece.strip().split(" as ")[-1].strip())
    # Parameters and destructured locals, taken loosely - this check is looking for
    # calls to functions that do not exist, so over-collecting names is the safe
    # direction. Under-collecting would produce false alarms and get the test deleted.
    for match in re.finditer(r"\(([^)]*)\)\s*=>", source):
        for piece in re.split(r"[,{}\[\]:()]", match.group(1)):
            token = piece.strip().split("=")[0].strip()
            if token.isidentifier():
                names.add(token)
    for match in re.finditer(r"function\s*[A-Za-z_$\w]*\s*\(([^)]*)\)", source):
        for piece in re.split(r"[,{}\[\]:()]", match.group(1)):
            token = piece.strip().split("=")[0].strip()
            if token.isidentifier():
                names.add(token)
    # Class methods. `absorb(rows) {` inside a class body is a declaration, but it
    # looks nothing like `function absorb` - and the Confluence scene is a class, so
    # without this every one of its thirteen methods reads as an undefined call.
    for match in re.finditer(r"^\s{2,}(?:async\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{", source, re.M):
        names.add(match.group(1))
    return names


# --------------------------------------------------------------------------- #
# The check that would have caught the bug
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_every_function_called_is_actually_defined(path: Path) -> None:
    """`paintOffline` was called and never defined. Nothing noticed for a whole
    milestone, because nothing looked at this directory."""
    source = read(path)
    scannable = code_only(source)
    available = declared_names(source) | _GLOBALS

    undefined = sorted(
        {
            name
            for name in _CALLS.findall(scannable)
            if name not in available and not name[0].isupper()
        }
    )
    assert undefined == [], (
        f"{path.relative_to(ROOT)} calls {undefined}, which nothing defines or imports"
    )


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_every_module_parses(path: Path) -> None:
    """A syntax error in a screen is invisible until someone opens that screen."""
    node = subprocess.run(
        ["node", "--version"], capture_output=True, text=True, shell=False
    )
    if node.returncode != 0:
        pytest.skip("node is not available")

    source = read(path)
    stripped = re.sub(r"^export ", "", source, flags=re.M)
    stripped = re.sub(r"^import .*$", "", stripped, flags=re.M)
    probe = (
        "try { new Function(require('fs').readFileSync(process.argv[1],'utf8')); }"
        "catch (e) { console.error(e.message); process.exit(1); }"
    )
    temporary = ROOT / f".parse-probe-{path.stem}.js"
    temporary.write_text(stripped, encoding="utf-8")
    try:
        result = subprocess.run(
            ["node", "-e", probe, str(temporary)], capture_output=True, text=True
        )
    finally:
        temporary.unlink(missing_ok=True)
    assert result.returncode == 0, f"{path.name}: {result.stderr.strip()[:200]}"


# --------------------------------------------------------------------------- #
# The screen contract
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", SCREENS, ids=lambda p: p.stem)
def test_every_screen_satisfies_the_contract(path: Path) -> None:
    source = read(path)
    assert re.search(r"export function render\(container, state", source), (
        f"{path.name} must export render(container, state, helpers)"
    )
    assert "export const meta" in source, f"{path.name} must export meta"


@pytest.mark.parametrize("path", SCREENS, ids=lambda p: p.stem)
def test_every_screen_teaches_in_its_empty_state(path: Path) -> None:
    """An empty state that does not say what would appear here, and how to make it
    appear, is a dead end."""
    source = read(path)
    assert 'class="empty"' in source, f"{path.name} has no empty state"
    assert re.search(r"<code>[^<]*(make |python -m )", source), (
        f"{path.name}'s empty state gives no command"
    )


@pytest.mark.parametrize("path", SCREENS, ids=lambda p: p.stem)
def test_screens_escape_interpolated_text(path: Path) -> None:
    """Narrations are merchant-supplied. Every screen that renders them must escape."""
    source = read(path)
    if "innerHTML" not in source:
        return
    assert "esc(" in source, f"{path.name} builds HTML without ever calling esc()"


@pytest.mark.parametrize("path", SCREENS, ids=lambda p: p.stem)
def test_wide_tables_scroll_inside_their_own_container(path: Path) -> None:
    """The page itself must never scroll sideways."""
    source = read(path)
    if "<table" not in source:
        return
    assert "scroll-x" in source, f"{path.name} has a table outside a .scroll-x wrapper"


# --------------------------------------------------------------------------- #
# House rules
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", SCREENS, ids=lambda p: p.stem)
def test_no_raw_animate_without_a_reduced_motion_guard(path: Path) -> None:
    """Every animation must be a no-op for a user who asked for that. The motion.js
    helpers handle it; a direct `.animate()` has to check for itself."""
    source = read(path)
    if ".animate(" not in source:
        return
    assert "reduced()" in source, (
        f"{path.name} calls .animate() directly but never checks reduced()"
    )


def test_no_screen_hard_codes_a_cost_parameter() -> None:
    """The alpha screen carried its own copy of the cost model and got it wrong by
    5.26x, so the slider's rupee figure disagreed with `scripts/calibrate.py` for the
    same operating point. Costs are served by /costmodel now; a screen that reaches
    for a literal has reintroduced the bug."""
    for path in SCREENS:
        source = read(path)
        offenders = [
            line.strip()
            for line in source.splitlines()
            if re.search(r"COST_PER_\w+\s*=\s*\d", line) and "FALLBACK" not in line
        ]
        assert offenders == [], f"{path.name} hard-codes a cost: {offenders}"


def test_the_alpha_grid_reaches_the_values_the_readme_argues_about() -> None:
    """The README's headline operating point is α=1% and its most-quoted caveat is the
    α=2% breach. A slider that cannot land on either describes a different system."""
    source = read(SCREENS_DIR / "alpha.js")
    anchors = re.search(r"const ANCHORS = \[([^\]]+)\]", source)
    assert anchors, "alpha.js no longer anchors its grid"
    values = {float(v.strip()) for v in anchors.group(1).split(",") if v.strip()}
    assert 0.01 in values and 0.02 in values


# --------------------------------------------------------------------------- #
# The import graph
# --------------------------------------------------------------------------- #
def test_every_import_resolves_to_a_real_export() -> None:
    exports: dict[Path, set[str]] = {}
    for path in MODULES:
        source = read(path)
        names: set[str] = set()
        for pattern in (
            r"export (?:async )?function (\w+)",
            r"export const (\w+)",
            r"export class (\w+)",
        ):
            names.update(re.findall(pattern, source))
        exports[path.resolve()] = names

    problems: list[str] = []
    for path in MODULES:
        source = read(path)
        for match in re.finditer(r"import\s*\{([^}]+)\}\s*from\s*[\"']([^\"']+)[\"']", source):
            target = (path.parent / match.group(2)).resolve()
            if not target.exists():
                problems.append(f"{path.name} imports a missing module {match.group(2)}")
                continue
            for piece in match.group(1).split(","):
                wanted = piece.strip().split(" as ")[0].strip()
                if wanted and wanted not in exports.get(target, set()):
                    problems.append(f"{path.name} imports {wanted}, not exported by {match.group(2)}")
    assert problems == [], problems


def test_the_router_knows_every_screen_file() -> None:
    source = read(WEB / "js" / "app.js")
    registered = set(re.findall(r'import\("\./screens/(\w+)\.js"\)', source))
    on_disk = {path.stem for path in SCREENS}
    assert registered == on_disk, (
        f"router registers {sorted(registered)} but disk has {sorted(on_disk)}"
    )


# --------------------------------------------------------------------------- #
# Served by the API
# --------------------------------------------------------------------------- #
def test_every_dashboard_asset_is_served() -> None:
    """Zero build step is only true if the API actually serves every file."""
    from fastapi.testclient import TestClient

    from api.main import app

    paths = ["/app/", "/app/styles/tokens.css", "/app/styles/app.css", "/app/styles/screens.css"]
    paths += [f"/app/js/{path.name}" for path in sorted((WEB / "js").glob("*.js"))]
    paths += [f"/app/js/screens/{path.name}" for path in SCREENS]

    with TestClient(app) as client:
        for path in paths:
            assert client.get(path).status_code == 200, path


def test_the_costmodel_endpoint_matches_the_python_model() -> None:
    """The endpoint exists so the dashboard cannot drift from core/costmodel.py."""
    from fastapi.testclient import TestClient

    from api.main import app
    from core.costmodel import CostModel
    from core.money import Money

    with TestClient(app) as client:
        payload = client.get("/costmodel").json()

    model = CostModel()
    assert payload["cost_per_false_post_paise"] == model.cost_per_false_match(
        Money.from_rupees("10000")
    ).paise
    assert payload["cost_per_review_paise"] == model.cost_per_review().paise
    assert all(item["illustrative"] for item in payload["assumptions"])


def test_tokens_define_both_themes_completely() -> None:
    """Neither theme may be a patch on the other, and an explicit choice must beat
    the system preference in both directions."""
    tokens = read(WEB / "styles" / "tokens.css")
    assert "prefers-color-scheme: dark" in tokens
    assert ':root[data-theme="dark"]' in tokens
    assert ':root:not([data-theme="light"])' in tokens
    assert "prefers-reduced-motion: reduce" in tokens


def test_the_seal_reports_the_real_residual() -> None:
    """`formatINR(0)` meant the balanced seal said "₹0.00" whether or not it was — on
    the one screen whose entire claim is that the waterfall closes to the paise."""
    source = read(SCREENS_DIR / "waterfall.js")
    assert "formatINR(\n      0\n    )" not in source
    assert "balanced: raw.balanced === true" in source, (
        "an absent `balanced` field must not read as balanced"
    )
