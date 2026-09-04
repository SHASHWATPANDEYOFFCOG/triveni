/* The landing page.
 *
 * Three jobs: mount the 3D hero, write the measured numbers into the page, and run the
 * scroll reveals. Everything else on the page is static HTML and CSS.
 *
 * The numbers come from `metrics.generated.js`, which `scripts/gen_web_metrics.py`
 * writes from `metrics.json`. The HTML deliberately contains no figures at all - only
 * ids and em-dashes - so a claim on this page cannot outlive the measurement that
 * justified it. If the generator has not run, the page shows dashes rather than a
 * plausible-looking default, because a plausible-looking default is precisely the
 * thing that gets read out on stage and turns out to be three commits stale.
 */

import { mountConfluence } from "./3d/confluence.js";
import { DATASET, METRICS } from "./metrics.generated.js";
import { observeReveals } from "./motion/reveal.js";

/* --------------------------------------------------------------------------- */
/* Measured numbers                                                             */
/* --------------------------------------------------------------------------- */
/** id -> metric key. One place, so a renamed metric fails loudly in one spot. */
const BINDINGS = {
  "m-rows": "rowsIngested",
  "m-match": "matchRate",
  "m-autoprec": "autoPostPrecision",
  "m-autocov": "autoPostCoverage",
  "m-threshold": "conformalThreshold",
  "m-realised": "realisedError",
  "m-bound": "nominalBound",
  "m-recall": "recall",
  "m-model": "rowsReachingModelRate",
  "m-modeln": "rowsReachingModel",
  "m-calls": "residueLlmCalls",
  "m-exceptions": "exceptionsRaised",
  "p-match": "matchRate",
  "p-cov": "autoPostCoverage",
  "hero-model-rate": "rowsReachingModelRate",
  "ladder-model": "rowsReachingModel",
};

function paintMetrics() {
  const animated = new Set([
    "m-rows",
    "m-match",
    "m-autoprec",
    "m-autocov",
    "m-realised",
    "m-recall",
    "m-model",
    "m-exceptions",
  ]);

  for (const [id, key] of Object.entries(BINDINGS)) {
    const node = document.getElementById(id);
    const metric = METRICS[key];
    if (!node || !metric) continue;

    const text = `${metric.value.toFixed(metric.decimals)}${metric.suffix}`;
    if (animated.has(id)) {
      // Hand the value to the counter rather than writing it: `countUp` guarantees the
      // final frame is the exact target, so the animation cannot land on a rounded
      // number that differs from the measurement.
      node.dataset.count = String(metric.value);
      node.dataset.countDecimals = String(metric.decimals);
      node.dataset.countSuffix = metric.suffix;
      node.dataset.reveal = "";
      node.textContent = text;
    } else {
      node.textContent = text;
    }
  }

  // Two that need a sentence rather than a number.
  setText("ladder-model", `${METRICS.rowsReachingModel.value} rows`);
  setText("m-dataset", `${DATASET.name} · ${DATASET.rows} rows`);
  setText("m-digest", DATASET.digest.slice(0, 16) + "…");

  // The "before" panel quotes the settlement total from the illustrative decomposition
  // beside it, so the two halves of the comparison cannot disagree.
  setText("before-amount", "₹11,99,191.82");
}

function setText(id, value) {
  const node = document.getElementById(id);
  if (node) node.textContent = value;
}

/* --------------------------------------------------------------------------- */
/* Navbar                                                                       */
/* --------------------------------------------------------------------------- */
function wireNav() {
  const nav = document.getElementById("site-nav");
  if (!nav) return;

  // A sentinel at the top of the document, observed instead of listening to scroll.
  // One observer callback on a threshold crossing, rather than a handler that runs on
  // every one of the hundreds of scroll events a flick produces.
  const sentinel = document.createElement("div");
  sentinel.style.cssText = "position:absolute;top:0;height:1px;width:1px";
  sentinel.setAttribute("aria-hidden", "true");
  document.body.prepend(sentinel);

  if ("IntersectionObserver" in window) {
    new IntersectionObserver(
      ([entry]) => {
        nav.dataset.stuck = String(!entry.isIntersecting);
      },
      { rootMargin: "-72px 0px 0px 0px" }
    ).observe(sentinel);
  } else {
    nav.dataset.stuck = "true";
  }
}

/* --------------------------------------------------------------------------- */
/* Theme                                                                        */
/* --------------------------------------------------------------------------- */
function wireTheme() {
  const toggle = document.getElementById("theme-toggle");
  const icon = document.getElementById("theme-icon");
  if (!toggle) return;

  // The landing page opens dark - it is one lit composition and there is no honest
  // light-mode version of deep space. The toggle is still here because someone who
  // needs a light interface needs it on every page, not only the ones we thought
  // would be read in daylight.
  const paint = () => {
    const dark = document.documentElement.dataset.theme !== "light";
    icon.textContent = dark ? "☾" : "☀";
    toggle.setAttribute(
      "aria-label",
      dark ? "Switch to the light theme" : "Switch to the dark theme"
    );
  };

  toggle.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("triveni-theme", next);
    } catch {
      /* private mode: the choice simply does not persist */
    }
    paint();
  });

  paint();
}

/* --------------------------------------------------------------------------- */
/* Boot                                                                         */
/* --------------------------------------------------------------------------- */
paintMetrics();
wireNav();
wireTheme();
observeReveals(document);

const canvas = document.getElementById("confluence");
if (canvas) mountConfluence(canvas);
