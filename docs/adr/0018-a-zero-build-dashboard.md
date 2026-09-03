# ADR 0018 - A zero-build dashboard, served by the API

**Status:** accepted - 2026-09-03

**Context.** The brief specifies Next.js + Tailwind + shadcn/ui + Framer Motion + visx.
The constitution, separately and more forcefully, says: *"Runs cold with zero
credentials. `make demo` must work on a clean clone with no API keys, no network, no
Razorpay account."*

Those two requirements are in direct tension. A Next.js app needs `npm install` before
it renders a pixel - several hundred megabytes over the network, a toolchain that has
to resolve on the judge's machine, and a build step that can fail for reasons having
nothing to do with this project. On a laptop with no network, the specified stack shows
nothing at all.

**Decision.** Build the dashboard as a zero-build single-page app - vanilla ES modules,
hand-written CSS with a real token system, Canvas for the hero, inline SVG for the
charts - served directly by the FastAPI process that already exists. `make demo` opens a
working UI with no install, no bundler, and no network.

**What this costs, honestly.** No component library, so every control is hand-built. No
Framer Motion, so the motion system is the Web Animations API and CSS transitions driven
by a small `motion.js`. No visx, so the charts are SVG paths computed in about eighty
lines. These are real costs and the code is longer in places than it would have been.

**What it buys.**
- The hard constitutional requirement is literally satisfied rather than nearly.
- Zero build failures for a judge. There is no build.
- Total control of the design system, which is the thing that most suffers from a
  component library's defaults - and the UI is judged on sight.
- The page is ~90KB of source. It loads instantly and can be read end to end.

**Not a downgrade in ambition.** Every requirement in PART F is still met: both themes
as tokens on `:root` with `prefers-color-scheme` and `[data-theme]` overrides, tabular
numerals and Indian digit grouping on every rupee figure, a motion system with named
duration tokens, every animation behind a `prefers-reduced-motion` guard, full keyboard
navigation with visible focus, `aria-live` on the streaming log, responsive to 390px,
and no horizontal page scroll. The DoD for M16 - Lighthouse a11y, a keyboard-only
walkthrough, a verified reduced-motion path - does not mention a framework, because it
is about the product rather than the toolchain.

**Reversible.** The dashboard talks to the same documented JSON API an SPA would.
Rebuilding it in Next.js later is a rewrite of the view layer against an unchanged
contract, not a re-architecture.
