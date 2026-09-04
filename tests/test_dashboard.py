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
import shutil
import subprocess
import sys
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
    # Two components satisfy this: the original `.empty` and the newer `.state-block`,
    # which adds a glyph and tone variants. Either is a real empty state; what the
    # test is actually about is the two assertions below it.
    assert 'class="empty"' in source or 'class="state-block"' in source, (
        f"{path.name} has no empty state"
    )
    # It must name what would appear here...
    assert re.search(r"<h3>[^<]+</h3>", source), (
        f"{path.name}'s empty state does not say what is missing"
    )
    # ...and give the reader a way out, either a command to run or a control to press.
    assert re.search(r"<code>[^<]*(make |python -m )", source) or re.search(
        r"<strong>[^<]+</strong>|class=\"btn", source
    ), f"{path.name}'s empty state is a dead end - no command and no control"


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

    # Derived from the files on disk and from what the HTML actually asks for, rather
    # than a hand-kept list. A hand-kept list is how a new stylesheet ships unserved:
    # the page 404s one file, the layout quietly degrades, and the test stays green.
    paths = ["/app/"]
    paths += [f"/app/{p.relative_to(WEB).as_posix()}" for p in sorted(WEB.rglob("*.css"))]
    paths += [f"/app/{p.relative_to(WEB).as_posix()}" for p in sorted(WEB.rglob("*.js"))]

    for page in sorted(WEB.glob("*.html")):
        paths.append(f"/app/{page.name}")
        for ref in re.findall(r'(?:href|src)="\./([^"]+)"', read(page)):
            paths.append(f"/app/{ref}")

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


def test_hidden_actually_hides() -> None:
    """The bug that made the whole dashboard look dead.

    `el.hidden = true` relies on `[hidden] { display: none }`, which ships in the
    *user-agent* stylesheet. Author rules beat the UA stylesheet by origin - specificity
    is never consulted - so `.help-overlay { display: grid }` quietly won. That overlay
    is `position: fixed; inset: 0; z-index: 100`, so it covered every screen from first
    paint, and neither Escape nor its close button could dismiss it: both set `.hidden`,
    which by then meant nothing. `.btn { display: inline-flex }` did the same to the
    audit screen, showing "tamper" and "restore" at once.

    So the guard has to be `!important` - it must beat component rules that do not exist
    yet, in a stylesheet loaded after this one.
    """
    # Comments first: this rule is explained in a comment that quotes the very selector
    # being searched for, so an unstripped scan passes on the prose alone.
    sheets = {
        path.name: _strip_css_comments(read(path))
        for path in sorted((WEB / "styles").glob("*.css"))
    }
    guards = [
        match.group(0)
        for css in sheets.values()
        for match in re.finditer(r"\[hidden\]\s*\{[^}]*\}", css)
    ]
    assert guards, "some stylesheet must define a global [hidden] rule"
    assert any(re.search(r"display:\s*none\s*!important", g) for g in guards), (
        "[hidden] must be !important, or any component setting `display` defeats it"
    )

    # Non-vacuity, stated as the general trap rather than one selector: some author
    # rule still sets `display` on a class that JS toggles with `.hidden`. While that
    # is true, deleting the guard reintroduces an undismissable overlay.
    toggled = {"overlay", "palette", "btn", "rail", "scrim"}
    trapped = {
        name
        for name in toggled
        for css in sheets.values()
        if re.search(rf"\.{name}\s*(?:,[^{{]*)?\{{[^}}]*display:", css)
    }
    assert trapped, (
        "no class that JS hides via `.hidden` sets `display` any more - this test now "
        "proves nothing and should be re-pointed at a rule that does"
    )
    assert 'id="help"' in read(WEB / "index.html")
    assert "element.hidden = true" in read(WEB / "js" / "ui" / "dialog.js")


def test_the_help_dialog_can_be_dismissed_every_way_it_is_opened() -> None:
    """Escape, the close button and a click on the backdrop must all reach `closeHelp`,
    and `closeHelp` must clear the attribute *before* it moves focus - so a throw on the
    focus call cannot strand the dialog open on top of the page."""
    source = read(WEB / "js" / "ui" / "dialog.js")

    # The four obligations a modal owes a keyboard user. Every dialog in the product
    # goes through this one module, so asserting them here covers all of them - and
    # anything added later gets them by construction rather than by remembering.
    assert 'event.key === "Escape"' in source, "Escape must close"
    assert 'event.key !== "Tab"' in source, "Tab must be trapped inside the dialog"
    assert "requestAnimationFrame(() => target.focus?.())" in source, (
        "focus must move into the dialog, or a keyboard user is stranded outside it"
    )
    assert "if (event.target === element) close()" in source, "clicking the scrim closes"
    assert "opener?.isConnected" in source, (
        "focus must return to the opener, and only while it is still in the document"
    )

    body = source[source.index("function close()") :]
    body = body[: body.index("\n  }")]
    assert body.index("element.hidden = true") < body.index("opener?.isConnected"), (
        "clear `hidden` before restoring focus, so a focus failure cannot strand the "
        "dialog open on top of the page"
    )

    # And the shell must actually route its dialogs through it.
    shell = read(WEB / "js" / "app.js")
    assert "openDialog(overlay" in shell, "the help sheet must use the shared dialog"
    assert "openDialog(root" in read(WEB / "js" / "ui" / "palette.js"), (
        "the command palette must use the shared dialog"
    )


def _strip_css_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def test_every_design_token_referenced_is_defined() -> None:
    """A `var(--typo)` fails silently - the property is simply dropped, and the element
    renders with an inherited or initial value that often looks *almost* right.

    This is the failure mode a re-skin invites: token names get re-pointed, one consumer
    is missed, and a single card keeps a colour from the old identity. Nothing throws,
    nothing logs, and it is invisible unless you happen to look at that card.
    """
    tokens = _strip_css_comments(read(WEB / "styles" / "tokens.css"))
    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", tokens))
    assert len(defined) > 100, "tokens.css should define the whole system"

    unresolved: dict[str, set[str]] = {}
    for path in [*WEB.rglob("*.css"), *WEB.rglob("*.js"), *WEB.rglob("*.html")]:
        if path.name == "tokens.css":
            continue
        text = _strip_css_comments(read(path))
        # A file may define its own scoped custom properties; those are legitimate.
        local = set(re.findall(r"(--[a-z0-9-]+)\s*:", text))
        # `var(--x, fallback)` is not an unresolved reference - the fallback IS the
        # definition, and that is the whole point of the syntax. Only a bare `var(--x)`
        # with nothing behind it fails silently, dropping the declaration and leaving
        # the element on an inherited value that often looks almost right.
        for name, delimiter in re.findall(r"var\(\s*(--[a-z0-9-]+)\s*([,)])", text):
            if delimiter == ",":
                continue
            if name not in defined and name not in local:
                unresolved.setdefault(name, set()).add(path.name)

    assert not unresolved, f"var() references with no definition: {unresolved}"


def test_the_token_system_covers_every_category() -> None:
    """Tokens are only a system if nothing has to reach outside them. Each category
    below exists because a component needed it and would otherwise hard-code a literal."""
    tokens = _strip_css_comments(read(WEB / "styles" / "tokens.css"))
    for name in (
        "--space-4",       # spacing scale
        "--radius-md",     # radius scale
        "--shadow-3",      # elevation
        "--blur-lg",       # depth
        "--dur-micro",     # motion
        "--ease-spring",   # motion curves
        "--z-modal",       # stacking
        "--bp-lg",         # breakpoints
        "--glass-bg",      # glass
        "--aurora-1",      # background
        "--glow-accent",   # glow
        "--text-display",  # type scale
        "--scrim",         # overlay
    ):
        assert f"{name}:" in tokens, f"{name} must be a token, not a literal"


def test_brand_colour_never_carries_state_meaning() -> None:
    """Green, amber and red mean one thing each. If the brand blue could also mean
    "healthy", the screen becomes unreadable the moment something goes wrong, because
    the eye can no longer separate decoration from alarm.

    So the six row states must resolve to the semantic ramps, never to blue or violet -
    with one deliberate exception: `auto-posted` IS blue, because "the machine did this
    unsupervised" is a statement about *who acted*, not about whether it went well.
    """
    tokens = _strip_css_comments(read(WEB / "styles" / "tokens.css"))
    ramps = dict(re.findall(r"(--[a-z]+-\d{3})\s*:\s*(#[0-9a-fA-F]{6})", tokens))

    def hue_of(value: str) -> tuple[float, float] | None:
        """(hue in degrees, saturation) for a token value, or None if not a colour.

        Spelling is not the property under test - the dark theme writes
        `rgba(34, 197, 94, 0.14)` where the light theme writes `var(--green-500)`, and
        those are the same colour. Only the hue can tell you whether a state token has
        drifted onto the brand ramp.
        """
        value = value.strip()
        if value.startswith("var("):
            value = ramps.get(value[4:].split(")")[0].strip(), "")
        if match := re.match(r"#([0-9a-fA-F]{6})$", value):
            rgb = [int(match.group(1)[i : i + 2], 16) for i in (0, 2, 4)]
        elif match := re.match(r"rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)", value):
            rgb = [int(g) for g in match.groups()]
        else:
            return None
        r, g, b = (c / 255 for c in rgb)
        high, low = max(r, g, b), min(r, g, b)
        span = high - low
        if span == 0:
            return 0.0, 0.0
        if high == r:
            hue = 60 * (((g - b) / span) % 6)
        elif high == g:
            hue = 60 * ((b - r) / span + 2)
        else:
            hue = 60 * ((r - g) / span + 4)
        return hue, span / high

    def in_band(hue: float, low: float, high: float) -> bool:
        return low <= hue <= high if low <= high else hue >= low or hue <= high

    bands = {"green": (90.0, 165.0), "amber": (20.0, 60.0), "red": (345.0, 15.0)}

    # The four states that carry semantic colour must land in their own hue band.
    # `auto-posted` is deliberately blue: "the machine did this unsupervised" is a
    # statement about who acted, not about whether it went well.
    for state, band in (
        ("--state-matched", bands["green"]),
        ("--state-needs-review", bands["amber"]),
        ("--state-exception", bands["red"]),
        ("--state-auto-posted", (195.0, 255.0)),
    ):
        values = re.findall(rf"{state}:\s*([^;]+);", tokens)
        assert values, f"{state} must be defined in every theme"
        for value in values:
            resolved = hue_of(value)
            assert resolved is not None, f"{state} = {value.strip()!r} is not a colour"
            hue, saturation = resolved
            assert saturation > 0.3, f"{state} = {value.strip()!r} is too washed out to read"
            assert in_band(hue, *band), (
                f"{state} resolves to hue {hue:.0f}deg, outside {band} - a state colour "
                "has drifted onto the wrong ramp"
            )

    # And the two neutral states must never *impersonate* a semantic one.
    for state in ("--state-abstained", "--state-denied"):
        for value in re.findall(rf"{state}:\s*([^;]+);", tokens):
            resolved = hue_of(value)
            assert resolved is not None, f"{state} = {value.strip()!r} is not a colour"
            hue, saturation = resolved
            if saturation <= 0.35:
                continue  # neutral by construction
            for name, band in bands.items():
                assert not in_band(hue, *band), (
                    f"{state} resolves to a saturated {name} (hue {hue:.0f}deg) and will "
                    "read as a state it does not mean"
                )


# --------------------------------------------------------------------------- #
# The landing page, and the numbers on it
# --------------------------------------------------------------------------- #
def test_the_landing_page_numbers_match_metrics_json() -> None:
    """`make eval` produces the numbers; a generator writes them into a module; the
    page imports it. This asserts the committed module has not drifted from
    `metrics.json` - which is the only way a figure on a marketing page can quietly
    outlive the measurement that justified it."""
    result = subprocess.run(
        [sys.executable, "-m", "scripts.gen_web_metrics", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_landing_page_hard_codes_no_measured_figure() -> None:
    """The page must contain ids, not numbers.

    A figure typed into the HTML is a figure that survives its own refutation. Every
    measured value is written in by `landing.js` from the generated module, so this
    asserts none of them appears as a literal in the markup - if one did, `make eval`
    could change it and the page would keep saying the old thing.
    """
    import json

    html = read(WEB / "landing.html")
    payload = json.loads(read(ROOT / "metrics.json"))

    offenders = []
    for row in payload["metrics"]:
        value = float(row["value"])
        # The rendered forms a hand-typed claim would take.
        candidates = {f"{value * 100:.2f}%", f"{value:.4f}"}
        if value >= 1:
            candidates.add(f"{value:,.0f}")
            candidates.add(f"{value:.0f}")
        for text in candidates:
            # Two characters of context, so a bare "536" inside an unrelated string
            # (a colour, a digest) is not mistaken for the row count.
            if text in html and len(text) > 3:
                offenders.append((row["name"], text))

    assert not offenders, (
        f"measured figures typed into landing.html: {offenders}. "
        "Bind them to an id and let scripts/gen_web_metrics.py supply the value."
    )


def test_the_landing_page_binds_every_id_it_declares() -> None:
    """Every `id="m-…"` placeholder in the HTML must be written to by landing.js, and
    every binding in landing.js must have somewhere to land. Either half alone leaves
    a silent em-dash on the page."""
    html = read(WEB / "landing.html")
    script = read(WEB / "js" / "landing.js")

    placeholders = set(re.findall(r'id="(m-[a-z0-9-]+|p-[a-z-]+|hero-model-rate|ladder-model|before-amount)"', html))
    written = set(re.findall(r'"(m-[a-z0-9-]+|p-[a-z-]+|hero-model-rate|ladder-model|before-amount)"', script))

    assert placeholders - written == set(), (
        f"ids in the HTML that nothing ever fills: {sorted(placeholders - written)}"
    )
    assert written - placeholders == set(), (
        f"landing.js writes to ids that do not exist: {sorted(written - placeholders)}"
    )


# --------------------------------------------------------------------------- #
# The 3D engine
# --------------------------------------------------------------------------- #
def test_the_3d_maths_is_correct() -> None:
    """Projection and rotation, checked as arithmetic under plain Node.

    This is the answer to the gap that let an undismissable overlay ship past 74
    static tests: rendering bugs are invisible to source analysis. Projection maths is
    the part of a 3D scene that can be wrong in a way that still draws something
    plausible - an inverted axis, a scale that grows with distance, a point behind the
    camera mirrored onto the screen - and every one of those is checkable without a
    browser, which is exactly why this scene is 120 lines of arithmetic rather than a
    library.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    result = subprocess.run(
        [node, str(WEB / "js" / "3d" / "engine.test.mjs")],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "assertions passed" in result.stdout


def test_the_hero_scene_degrades_on_every_axis() -> None:
    """A hero that costs a phone its battery is a bad trade for a first impression."""
    source = read(WEB / "js" / "3d" / "confluence.js")

    assert 'canvas.getContext?.("2d")' in source, (
        "a missing 2D context must leave the CSS fallback, not throw"
    )
    assert "prefers-reduced-motion" in source, "reduced motion must compose one frame"
    assert "IntersectionObserver" in source, "off-screen must pause the loop"
    assert "visibilitychange" in source, "a hidden tab must pause the loop"
    assert "hardwareConcurrency" in source, "particle count must scale to the device"
    assert "Math.min(window.devicePixelRatio || 1, 2)" in source, (
        "device pixel ratio must be capped, or a 3x phone paints 9x the pixels"
    )
    assert "Math.min(48, now - (last || now))" in source, (
        "the frame delta must be clamped, or returning to a backgrounded tab "
        "teleports every particle in a single frame"
    )
    # And the fallback must actually be a composed picture rather than an empty box.
    assert "radial-gradient" in read(WEB / "styles" / "landing.css")


# --------------------------------------------------------------------------- #
# Accessibility
# --------------------------------------------------------------------------- #
def _srgb_to_linear(channel: float) -> float:
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def _luminance(hex_colour: str) -> float:
    """WCAG relative luminance."""
    value = hex_colour.lstrip("#")
    r, g, b = (int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return (
        0.2126 * _srgb_to_linear(r)
        + 0.7152 * _srgb_to_linear(g)
        + 0.0722 * _srgb_to_linear(b)
    )


def _contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    high, low = max(la, lb), min(la, lb)
    return (high + 0.05) / (low + 0.05)


def _composite(value: str, over: str) -> str:
    """Flatten a possibly-translucent colour onto an opaque one.

    The dark theme states its tints as `rgba(34, 197, 94, 0.14)`, which is the surface
    showing through. Scoring text against that raw value would measure a colour nobody
    ever sees - what the eye gets is the composite.
    """
    match = re.match(
        r"rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)(?:[,/\s]+([\d.]+))?\s*\)", value.strip()
    )
    if not match:
        return value.strip()
    r, g, b = (int(match.group(i)) for i in (1, 2, 3))
    alpha = float(match.group(4)) if match.group(4) else 1.0
    base = over.lstrip("#")
    br, bg, bb = (int(base[i : i + 2], 16) for i in (0, 2, 4))
    blend = lambda f, k: round(f * alpha + k * (1 - alpha))  # noqa: E731
    return f"#{blend(r, br):02x}{blend(g, bg):02x}{blend(b, bb):02x}"


def _resolve_theme(tokens: str, block: str) -> dict[str, str]:
    """Flatten one theme block into name -> #rrggbb, following var() chains.

    Ramps live on bare `:root`, and the dark blocks redefine only the semantic layer,
    so a dark theme's `--fg` may point at a ramp defined a hundred lines earlier. This
    resolves through that rather than giving up on the indirection.
    """
    ramps = dict(re.findall(r"(--[a-z]+-\d{3})\s*:\s*(#[0-9a-fA-F]{6})", tokens))

    if block == "root":
        body = tokens.split(":root {", 1)[1].split("\n}", 1)[0]
    else:
        body = tokens.split(block, 1)[1].split("\n}", 1)[0]

    raw = dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", body))
    # Semantic names not restated in a dark block inherit from :root.
    if block != "root":
        base = dict(
            re.findall(
                r"(--[a-z0-9-]+)\s*:\s*([^;]+);",
                tokens.split(":root {", 1)[1].split("\n}", 1)[0],
            )
        )
        raw = {**base, **raw}

    resolved: dict[str, str] = {}
    for name, value in raw.items():
        seen = 0
        while value.strip().startswith("var(") and seen < 6:
            value = raw.get(value.strip()[4:].split(")")[0].strip()) or ramps.get(
                value.strip()[4:].split(")")[0].strip(), ""
            )
            seen += 1
            if value is None:
                value = ""
        value = (value or "").strip()
        # rgba() is kept, not discarded. Dropping it silently skipped every state
        # colour in the dark theme, which is where they are all stated as tints.
        if re.fullmatch(r"#[0-9a-fA-F]{6}", value) or value.startswith("rgb"):
            resolved[name] = value
    return resolved


@pytest.mark.parametrize(
    "block", ["root", ':root[data-theme="dark"] {'], ids=["light", "dark"]
)
def test_text_meets_wcag_contrast_in_both_themes(block: str) -> None:
    """Contrast is arithmetic, so it is checkable without a browser.

    A palette can look right to the person who chose it and still be unreadable to
    someone reading it on a laptop in daylight. These are the pairings that actually
    carry text in the product; each is held to the WCAG AA ratio for its role - 4.5:1
    for body text, 3:1 for large text and for non-text boundaries.
    """
    tokens = _strip_css_comments(read(WEB / "styles" / "tokens.css"))
    theme = _resolve_theme(tokens, block)

    pairs = [
        ("--fg", "--bg", 4.5, "body text on the page"),
        ("--fg", "--bg-raised", 4.5, "body text on a card"),
        ("--fg-muted", "--bg", 4.5, "secondary text"),
        ("--fg-muted", "--bg-raised", 4.5, "secondary text on a card"),
        ("--fg-subtle", "--bg", 3.0, "labels and captions"),
        ("--accent", "--bg", 3.0, "links and the brand colour"),
        ("--accent", "--bg-raised", 3.0, "links on a card"),
        # `.money.pos` and `.money.neg` render these as ordinary small text on a card,
        # so they are held to 4.5:1 rather than to the 3:1 for large text.
        ("--ok", "--bg-raised", 4.5, "a positive amount"),
        ("--warn", "--bg-raised", 4.5, "a warning amount"),
        ("--bad", "--bg-raised", 4.5, "a negative amount"),
    ]

    # The six row states are the load-bearing colour in the product, and they are
    # drawn as 12px semibold text on their OWN tinted chip - not on the card. Checking
    # them against the card was checking a pairing that never appears on screen, and
    # it hid the fact that five of the six failed.
    for name in ("matched", "auto-posted", "needs-review", "abstained", "exception", "denied"):
        pairs.append((f"--state-{name}", f"--state-{name}-bg", 4.5, f"the {name} badge"))

    failures = []
    for foreground, background, minimum, what in pairs:
        if foreground not in theme or background not in theme:
            continue
        # A tinted chip is usually `rgba(...)`, which shows the surface through it.
        # Comparing against the raw token would score the text against a colour no eye
        # ever sees; compositing over the surface is what the reader actually gets.
        back = _composite(theme[background], theme.get("--bg-raised", "#ffffff"))
        front = _composite(theme[foreground], back)
        ratio = _contrast(front, back)
        if ratio < minimum:
            failures.append(
                f"{what}: {foreground} on {background} is {ratio:.2f}:1, "
                f"needs {minimum}:1"
            )

    assert not failures, "\n".join(failures)


@pytest.mark.parametrize(
    "page", sorted((Path(__file__).resolve().parent.parent / "web").glob("*.html")),
    ids=lambda p: p.name,
)
def test_every_control_has_an_accessible_name(page: Path) -> None:
    """A button whose only content is a glyph is, to a screen reader, a button called
    "☰". Every icon-only control has to carry its name some other way."""
    html = read(page)
    nameless = []
    for match in re.finditer(r"<button\b([^>]*)>(.*?)</button>", html, re.S):
        attrs, inner = match.group(1), match.group(2)
        if "aria-label" in attrs or "aria-labelledby" in attrs:
            continue
        # Text that is NOT inside an aria-hidden span counts as the name.
        visible = re.sub(r'<[^>]*aria-hidden="true"[^>]*>.*?</[^>]+>', "", inner, flags=re.S)
        if not re.sub(r"<[^>]+>", "", visible).strip():
            nameless.append(match.group(0)[:90])
    assert not nameless, f"controls with no accessible name in {page.name}: {nameless}"


@pytest.mark.parametrize(
    "page", sorted((Path(__file__).resolve().parent.parent / "web").glob("*.html")),
    ids=lambda p: p.name,
)
def test_decorative_and_structural_markup_is_sound(page: Path) -> None:
    html = read(page)

    # A positive tabindex reorders the whole document's tab sequence and is almost
    # always a mistake; -1 (programmatic focus) and 0 are fine.
    assert not re.search(r'tabindex="[1-9]', html), "no positive tabindex"

    # Canvas is decorative here - the data it draws is always stated in text nearby -
    # so it must be hidden from assistive tech rather than announced as an empty box.
    for match in re.finditer(r"<canvas\b([^>]*)>", html):
        assert "aria-hidden" in match.group(1) or "aria-label" in match.group(1), (
            f"canvas needs aria-hidden or a label: {match.group(0)}"
        )

    # Every page needs exactly one h1, and a skip link as the first focusable thing.
    assert html.count("<h1") <= 1, "at most one h1 per page"
    assert 'class="sr-only" href="#main"' in html, "every page needs a skip link"
    assert 'id="main"' in html


def test_reduced_motion_is_a_hard_switch() -> None:
    """Someone with vestibular sensitivity must still see everything - only the
    movement is withheld."""
    tokens = read(WEB / "styles" / "tokens.css")
    assert "prefers-reduced-motion: reduce" in tokens
    assert "animation-duration: 0.01ms !important" in tokens

    # And the pieces that animate in JavaScript must check it themselves, since a CSS
    # duration override does nothing to a requestAnimationFrame loop.
    for name in ("3d/confluence.js", "motion/reveal.js"):
        assert "prefers-reduced-motion" in read(WEB / "js" / name), (
            f"{name} animates in JS and must check the preference itself"
        )

    # Reveals must resolve to visible under reduced motion, never stay at opacity 0.
    components = _strip_css_comments(read(WEB / "styles" / "components.css"))
    guard = components.split("prefers-reduced-motion")[-1]
    assert ".reveal" in guard and "opacity: 1" in guard


def test_the_mobile_layout_is_designed_rather_than_shrunk() -> None:
    """Below 64rem the rail becomes a drawer and the table becomes cards. Neither is
    a smaller version of the desktop layout; both are different layouts."""
    app = _strip_css_comments(read(WEB / "styles" / "app.css"))
    components = _strip_css_comments(read(WEB / "styles" / "components.css"))

    assert "max-width: 64rem" in app, "the rail must collapse at the large breakpoint"
    assert "visibility: hidden" in app, (
        "a closed drawer must leave the tab order - a transform alone keeps every "
        "link inside it focusable behind the scrim"
    )
    assert 'data-rail="open"' in app

    assert "table.responsive thead" in components, "tables must reflow to cards"
    assert 'content: attr(data-label)' in components, (
        "each reflowed cell must carry its own label, or the card is a column of "
        "unlabelled values"
    )


# --------------------------------------------------------------------------- #
# Usability: does every screen tell you what to do?
# --------------------------------------------------------------------------- #
def test_every_screen_declares_guidance() -> None:
    """An audit found the dashboard had every number and no instructions.

    Guidance is mounted by the router, not by each screen, so a new screen gets it by
    construction. This asserts the declarations exist and are shaped as *instructions* -
    a statement of purpose plus things to press - rather than as another paragraph of
    description, which is what was already there and was not working.
    """
    from api.mcp_server import TOOLS  # noqa: F401  (import proves the app loads)

    guide = read(WEB / "js" / "ui" / "guide.js")
    router = read(WEB / "js" / "app.js")

    assert "mountGuide(container, name)" in router, (
        "the router must mount guidance, so no screen can forget to"
    )

    declared = set(re.findall(r"^  ([a-z]+): \{$", guide, re.M))
    screens = {path.stem for path in SCREENS}
    assert screens - declared == set(), (
        f"screens with no guidance: {sorted(screens - declared)}"
    )
    assert declared - screens == set(), (
        f"guidance for screens that do not exist: {sorted(declared - screens)}"
    )

    # Each entry needs a purpose and at least two concrete actions.
    for block in re.findall(r"\{\s*what: (.*?)\n  \},", guide, re.S):
        steps = re.findall(r'^\s{6}"', block, re.M)
        assert len(steps) >= 2, f"a guide with fewer than two actions is a description:\n{block[:120]}"

    # Dismissible and restorable, or it becomes furniture people stop reading.
    assert "localStorage" in guide and "resetGuides" in guide
    assert "help-reset-guides" in router, "the help sheet must be able to bring them back"


def test_every_screen_offers_a_primary_action() -> None:
    """Before this, only two of eight screens had a primary call to action - the
    alpha screen was a bare range input with no buttons at all. A screen with data
    and no affordance is a screen a judge looks at and then leaves."""
    missing = []
    for path in SCREENS:
        source = read(path)
        has_primary = "btn primary" in source
        # Some screens' primary affordance is a control group rather than a standing
        # button: triage's filter chips over a keyboard-driven queue, stages' segmented
        # ladder, and alpha's preset chips. Each is a real, obvious thing to press.
        has_control = any(
            token in source
            for token in ('class="chip"', "ladder-seg", "flow-seg", 'class="preset"')
        )
        if not (has_primary or has_control):
            missing.append(path.stem)
    assert not missing, f"screens with no primary action or interactive control: {missing}"


def test_the_alpha_screen_offers_the_values_the_project_argues_about() -> None:
    """A bare slider makes the reader guess where to put it. The three values the
    README actually argues about are one press away, and the presets drive the same
    slider rather than a parallel code path - so the two controls cannot disagree."""
    source = read(WEB / "js" / "screens" / "alpha.js")
    for alpha in ("0.01", "0.02", "0.05"):
        assert f'data-alpha="{alpha}"' in source, f"no preset for alpha={alpha}"
    assert 'aria-pressed' in source, "presets must report their pressed state"
    assert "slider.value = String(index)" in source, (
        "a preset must move the slider, not run a parallel path"
    )
    assert "alpha-reset" in source


def test_the_report_prints_without_the_navigation_chrome() -> None:
    """The print stylesheet hid `.header, .nav, .header-actions`. The sidebar layout
    renamed those to `.rail` / `.topbar`, so the entire navigation chrome printed on
    every page of the report and nothing noticed."""
    source = read(WEB / "js" / "screens" / "report.js")
    block = source[source.index("@media print") :]
    for selector in (".rail", ".topbar", ".backdrop"):
        assert selector in block, f"{selector} must be hidden when printing"
    # And the selectors it hides must actually exist in the shell.
    app = read(WEB / "styles" / "app.css")
    assert ".rail {" in app and ".topbar {" in app


def test_the_tooltip_component_is_actually_used() -> None:
    """It was built in the design-system pass and used exactly zero times, which is
    how a component library grows dead weight."""
    uses = sum('class="tip"' in read(path) for path in SCREENS)
    assert uses > 0, "the .tip component is defined but never used anywhere"
    assert ".term" in read(WEB / "styles" / "components.css"), (
        "an explained term needs a visible affordance, not just a hover target"
    )
