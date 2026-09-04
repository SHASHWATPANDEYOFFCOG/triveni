# ADR 0019 — A design system, and a 3D hero, without a framework

**Status:** accepted
**Supersedes nothing.** Extends [ADR 0018](0018-a-zero-build-dashboard.md).

## Context

The dashboard shipped as eight working screens on a functional but plain "financial
terminal" skin. It needed to look like a product rather than a tool: a landing page
that explains the problem, a real design system rather than ad-hoc CSS, motion, and a
3D hero.

The obvious way to build that is Next.js + React + Tailwind + shadcn/ui + React Three
Fiber + GSAP + Lenis. That is the stack the brief asked for, and it is a good stack.

It is also incompatible with a constraint this project has held since M0: **`make demo`
runs on a clean clone with no API keys, no network and no Razorpay account.** Every
library above arrives through `npm install`, which needs a network. Adopting them means
either retracting that guarantee or maintaining two frontends.

## Decision

Build the design system, the motion system and the 3D hero with no runtime
dependencies, keeping the zero-build guarantee intact.

- **Design tokens** — one file, 190+ tokens covering colour, type, space, radius,
  elevation, blur, motion, z-index and breakpoints, in two complete themes. This is
  what Tailwind's config would have held; it is a CSS file instead.
- **Components** — a hand-written library with every state (`components.css`). This is
  what shadcn/ui would have supplied. It is ~1,000 lines, and every line is reviewable.
- **Motion** — CSS transitions and keyframes off the token scale, plus one
  `IntersectionObserver` for scroll reveals. This is what GSAP + ScrollTrigger would
  have done for this page's needs, which are reveals, counters and parallax.
- **Smooth scroll** — `scroll-behavior: smooth`, not Lenis. See below.
- **3D** — a 120-line perspective engine rendering to Canvas 2D, instead of Three.js.

## Consequences

### What this buys

**The cold-clone guarantee survives.** No `npm install`, no `node_modules`, no build
step, no lockfile. The front end is 12 files the API already serves.

**The 3D maths is unit-tested.** This is the consequence that mattered most, and it was
not the original motivation. The project has no headless browser, and a `[hidden]` bug
had already taken the entire dashboard down while 74 static tests stayed green —
rendering failures are invisible to source analysis. Writing the projection by hand
made it fifteen assertions runnable under plain Node: a full turn is the identity,
rotation preserves length, scale falls monotonically with depth, a point behind the
camera is culled rather than mirrored, damping is frame-rate independent.

Every one of those is a bug that still *draws something plausible* when it is wrong. A
Three.js scene would have been correct by construction here, but a scene built on top of
it would have been just as untestable in this repo as the CSS was.

**The contrast is checkable.** Because the palette is tokens rather than utility
classes, a test can flatten each theme, composite the translucent chips over their
surface, and compute WCAG ratios. That test immediately found that **five of the six row
states failed AA for small text in the light theme** — between 2.86:1 and 4.44:1 against
a 4.5:1 requirement, on the most semantically loaded colour in the product. Fixed by
moving each to a darker ramp step; all twelve pairings now clear it with margin.

### What this costs

**No component library to inherit from.** Every state of every control was written by
hand. shadcn/ui would have supplied a better-tested dropdown, combobox and date picker
than anything here — none of which this product currently needs, but the next feature
might.

**No Three.js means no meshes, materials or real lighting.** The hero is a particle
field with painter's-algorithm depth sorting. That is the right tool for three streams
converging on a point, and it would be the wrong tool for anything with a surface.

**Smooth scrolling is the native property.** Lenis is genuinely smoother. It also
hijacks the wheel event and re-implements scrolling in JavaScript, which routinely
breaks keyboard paging, find-in-page and scroll restoration on back-navigation. For a
tool people use for hours, that trade is not worth taking.

**This is a defensible choice, not a universally correct one.** If the front end grows
past roughly a dozen screens, or if a second person joins it, or if the cold-clone
guarantee is ever dropped, the calculus changes and the framework becomes the right
answer. The cost of switching later is bounded: the tokens transfer directly to a
Tailwind config, and the component CSS transfers to whatever replaces it.

## Alternatives considered

**Vendor a minified Three.js into the repository.** Preserves the cold-clone guarantee
and gives real 3D. Rejected: it trades one honest sentence in the README for a 600KB
opaque blob nobody reviews, to solve a problem that turned out to be 120 lines of
arithmetic — and the arithmetic is testable in CI, which the blob would not have made
the surrounding scene.

**A second Next.js app alongside the existing dashboard.** Full spec, and `make demo`
keeps working because it uses the old one. Rejected: two dashboards means judges asking
which one is real, and it doubles the surface that has to stay correct.

**Replace the dashboard with Next.js outright.** Cleanest single codebase. Rejected: it
retracts the cold-clone guarantee, ADR 0018, and the "zero dependencies" line in the
submission — a large claim to give up for a stack choice nobody is scoring.
