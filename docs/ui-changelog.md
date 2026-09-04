# UI changelog

What changed in the front end, why, and what it cost. Newest first.

**New dependencies added across all of it: none.** No `npm install`, no `node_modules`,
no bundler, no lockfile, no webfont download. `make demo` still runs on a clean clone
with no network. See [ADR 0019](adr/0019-a-design-system-without-a-framework.md) for the
reasoning and for what that choice gives up.

---

## Usability pass — instructions, buttons, tooltips

**The audit that prompted it.** Counting affordances across the eight screens:

| | before | after |
|---|---|---|
| tooltips | **0** | in use, with a visible `.term` affordance |
| screens with a primary action | **2 of 8** | 8 of 8 |
| buttons on the α screen | **0** (a bare range input) | 3 presets + reset |
| per-screen instructions | **none** | all 8, dismissible and restorable |

Every number was on the page and nothing said what to do with it. That is fine for the
person who built it and useless for someone with four minutes.

### Per-screen guidance (`web/js/ui/guide.js`)

Each screen declares one line of purpose and two or three things to *press* — written as
instructions, not as another paragraph of description, because description is what was
already there and was not working.

Mounted **by the router**, not by each screen, so a new screen gets guidance by
construction rather than by someone remembering. It lands after the screen heading, not
before: the title tells you where you are, the guide tells you what to do once you know,
and putting instructions first would make every screen open on a block of text — the
exact thing being fixed.

Dismissible and remembered in `localStorage`, because instructions that cannot be turned
off become furniture and furniture is what people stop reading. Never *gone*, though —
dismissing leaves a small "show me how" chip in the same position on every screen, and
the help sheet has a button that restores all of them.

Keyboard shortcuts appear inside the relevant guide rather than only in a help dialog
nobody opens.

### Proper buttons

- **α screen** had zero buttons. A bare slider makes the reader guess where to put it, so
  the three values the project actually argues about — **1%** (the operating point), **2%**
  (breaches on one split), **5%** (loose) — are now one press away, with a reset. The
  presets drive the same slider rather than a parallel code path, so the two controls
  cannot disagree about where α is.
- **Report** gained a *Copy report* button beside *Print*, and the button reports its own
  outcome. Clipboard writes fail routinely — insecure origin, denied permission, older
  engine — and a copy button that does nothing visible is indistinguishable from one that
  worked, so failure says "select and press Ctrl+C" rather than nothing.
- **Audit**'s *Restore* was a ghost button next to a danger button; it is now the primary,
  because after simulating tampering, putting the record back is the thing you want.

### One real bug found and fixed

The report's print stylesheet hid `.header, .nav, .header-actions`. The sidebar layout had
renamed those to `.rail` / `.topbar`, so **the entire navigation chrome printed on every
page of the report** and nothing noticed. A test now asserts the print rules name
selectors that actually exist in the shell.

### One claim I got wrong

I said from a screenshot that nav item 8 (Report) was missing. It is present at
`web/index.html:73` — the screenshot was cropped below Audit. The rail's content needs
629px and fits down to a 640px viewport.

---

## Command centre — the first screen stops being a document

The screens measured out at **1,440 words of prose across 67 `<p>` tags**, and screen 1
opened with three sentences and two numbers.

Screen 1 is rebuilt so information is carried as shape, and every mark is proportional to
a real value:

- the canvas particle count **is** the row count (536 rows, 536 particles)
- the ring arc **is** auto-post coverage, with a tick at the calibrated threshold
- the flow segment widths **are** the measured per-stage milliseconds
- the bar lengths **are** exception counts by type, coloured by severity

The test worth applying to any dashboard: *if the picture would look identical against
different numbers, it is decoration.* Every element here would visibly change.

**The flow bar earns its place on its own.** Without a word of commentary it shows that
Fellegi–Sunter and the global assignment are **95% of an 8.7s close** — the honest cost of
solving the whole day at once instead of greedily. It defaults to selecting the most
expensive stage, because that is the one worth explaining.

**Less text without losing the argument.** The prose that was cut is the prose that
justifies the engineering — why MASE and never MAPE, what happens to a tampered record,
which cost parameters are assumed rather than sourced. Deleting it would make the product
look better and be worth less. So it is folded into `<details>`: native, keyboard-operable,
announced correctly, findable by in-page search while collapsed, working with no
JavaScript. Visible prose **1,440 → 1,203 words**, with 208 kept one click away.

**The landing preview is now real.** It was a skeleton mock; it now fetches an actual close
from the running API and renders the same flow bar with live timings, falling back to
measured figures when the API is down — with a label saying which of the two you are
looking at, so a static preview is never passed off as live. 2.5s timeout, because a
landing page must never sit waiting on a backend that may not be running.

---

## Design system

**190+ tokens in one file** — colour, type, space, radius, elevation, blur, motion,
z-index, breakpoints, glass, aurora, glow — in two complete themes, neither a patch on the
other. Token *names* were kept stable, so ~5,700 lines of existing screen CSS and JS
re-skinned at once instead of breaking a screen at a time.

A test asserts every `var()` in the product resolves (`var(--x, fallback)` correctly
exempt). Another asserts the six row states stay on their own hue ramps, so the brand blue
can never start meaning "healthy".

**Components**: buttons in 7 variants across 8 states, cards, inputs, toggle, checkbox,
slider, dropzone, badges, progress, table, timeline, toast, modal, drawer, tooltip,
skeleton, empty/error/success blocks, AI panels, and the command-centre set.

**Shell**: sidebar + topbar. Eight destinations past 1024px meant the old horizontal row
scrolled and two screens — including Audit — were undiscoverable. Below 64rem the rail
becomes a drawer with `visibility` toggled, so a closed drawer actually leaves the tab
order rather than keeping every link focusable behind the scrim. Command palette on
Ctrl-K with subsequence matching. One shared dialog module implements Escape, the focus
trap, the scrim click and the focus restore once, rather than four times with a different
subset each.

**Landing page**: fourteen sections. **No number on it is typed by hand** —
`scripts/gen_web_metrics.py` generates them from `metrics.json` into a module the page
imports, wired into `make eval`, with two tests failing the build if the page drifts from
the measurement or hard-codes a figure.

**3D hero**: three labelled streams converging on one settlement core, which is what a
close *is*. Written as a 120-line perspective engine on Canvas rather than pulled from
Three.js — and the deciding reason was **testability, not bundle size**. This project has
no headless browser, and a `[hidden]` bug had already taken the whole dashboard down past
74 green tests. The projection maths is fifteen assertions runnable under plain Node,
covering exactly the errors that still draw something plausible when wrong: an inverted
axis, a scale that grows with distance, a point behind the camera mirrored onto the screen.

The scene stops when off-screen, when the tab is hidden, and under reduced motion.
Particle count scales to `hardwareConcurrency`; device pixel ratio is capped at 2; the
frame delta is clamped so returning to a backgrounded tab does not teleport every particle.

### The accessibility finding

Because the palette is tokens, a test can flatten each theme, composite the translucent
chips over their surface, and compute WCAG ratios. It immediately found **five of the six
row states failing AA for small text in the light theme** — between 2.86:1 and 4.44:1
against a 4.5:1 requirement, on the most semantically loaded colour in the product,
shipping since M16. All twelve pairings now clear it; the worst is 5.02:1.

---

## What is still not covered

Every test here is **static**. They prove the source is self-consistent — that contrast
ratios computed from tokens are sound, that every identifier resolves, that the print rules
name real selectors. **None of them proves the browser agrees.** That is the same class of
blindness that let an undismissable modal ship, and it is closed only by a headless
browser this project does not have. `docs/limitations.md` is precise about which parts of
that gap these tests do and do not close.

No Lighthouse run and no cross-browser testing, so neither is claimed.
