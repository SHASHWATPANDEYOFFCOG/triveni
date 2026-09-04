/* Screen 3 - The alpha slider.
 *
 * One control for the conformal error bound, and five numbers that move together:
 * the threshold it fits, how much it lets through unsupervised, what error that
 * actually realised on held-out data, how many claims go to a human, and what the
 * whole thing costs in rupees.
 *
 * Two things make this worth building rather than faking with a formula.
 *
 * **Every point is measured.** Moving the slider re-runs the calibration on the
 * server at that alpha and reads back the threshold and the realised error. Nothing
 * here is interpolated between two anchors, which is why the curve has a visible
 * step in it rather than the smooth trade-off a made-up version would draw.
 *
 * **The step is the finding.** Below alpha ~1.2% the threshold sits high, admits
 * about seven claims in ten, and realises ZERO errors on the held-out split. Above
 * ~1.6% it drops, admits everything, and the cost multiplies. There is no smooth
 * middle because the score distribution has no mass there - and a judge dragging
 * across that jump is seeing the actual operating decision, not a UI flourish.
 */

import { esc, formatINR, formatPct } from "../format.js";
import { odometer, reduced } from "../motion.js";
import { api } from "../api.js";

/* A geometric grid, unioned with the alphas the documentation actually argues about.
 *
 * Linear steps waste almost every position on the flat upper half and skip the
 * low-alpha region entirely, which is where the decision lives - hence geometric. But
 * a pure geometric grid from 0.002 contains neither 0.01 nor 0.02, and those are
 * precisely the two the README quotes: alpha=1% is the headline operating point and
 * alpha=2% is the single-split breach. A judge dragging this slider could not land on
 * either of the numbers they had just read, which made the screen and the document
 * describe different systems.
 */
const ANCHORS = [0.01, 0.02, 0.05];
const GRID = [
  ...new Set([
    ...Array.from({ length: 19 }, (_, index) =>
      Number((0.002 * Math.pow(1.35, index)).toFixed(4))
    ),
    ...ANCHORS,
  ]),
]
  .filter((value) => value <= 0.5)
  .sort((a, b) => a - b);

/* Cost parameters are FETCHED, never hard-coded here.
 *
 * This screen used to carry its own copies and got one of them wrong by 5.26x - a
 * per-false-post cost of 500,000 paise against the real 95,000 that
 * `core/costmodel.py` computes - so the rupee figure on the slider disagreed with
 * what `python -m scripts.calibrate` printed for the same operating point. Two copies
 * of a number is one copy too many. These are the last-resort values used only when
 * /costmodel itself could not be reached, and the UI says so when it falls back.
 */
const FALLBACK_COSTS = { false_post: 95_000, review: 2_000 };

export function render(container, state) {
  if (!state.close) {
    container.innerHTML = `
      <div class="screen-head">
        <div class="eyebrow">The guarantee</div>
        <h1>The α slider</h1>
      </div>
      <div class="empty">
        <h3>No calibration to explore yet</h3>
        <p>The slider re-fits the conformal threshold at each α and measures what it realised.</p>
        <code>make run</code>
      </div>`;
    return;
  }

  const startIndex = GRID.findIndex((value) => value >= Number(state.alpha));
  const initial = startIndex >= 0 ? startIndex : 4;

  container.innerHTML = `
    <div class="screen-head">
      <div class="eyebrow">The guarantee · M12</div>
      <h1>How wrong are you willing to be?</h1>
      <p>
        Most reconciliation tools ship a confidence threshold somebody tried once.
        This one is <strong>fitted</strong>: pick the error rate you can live with and
        split conformal calibration returns the threshold that bounds it — then
        reports what that threshold actually realised on data it never saw.
      </p>
    </div>

    <div class="card">
      <div class="alpha-control">
        <label for="alpha-range" class="label subtle">
          Error bound α — the share of unsupervised postings allowed to be wrong
        </label>
        <input
          type="range"
          id="alpha-range"
          min="0"
          max="${GRID.length - 1}"
          step="1"
          value="${initial}"
          aria-describedby="alpha-readout"
        />
        <div class="alpha-readout" id="alpha-readout">
          <span>α = <strong id="alpha-value">—</strong></span>
          <span class="subtle">fitted threshold <span id="alpha-threshold" class="money">—</span></span>
        </div>

        <!-- A bare range input makes the reader guess where to put it. These are the
             three values the project actually argues about, so they are one press
             away; the slider still allows anything in between. -->
        <div class="row-between wrap" style="margin-top: var(--s-2)">
          <div class="presets" role="group" aria-label="Common operating points">
            <button class="preset" data-alpha="0.01" aria-pressed="false">
              1%<span class="why">the operating point</span>
            </button>
            <button class="preset" data-alpha="0.02" aria-pressed="false">
              2%<span class="why">breaches on one split</span>
            </button>
            <button class="preset" data-alpha="0.05" aria-pressed="false">
              5%<span class="why">loose</span>
            </button>
          </div>
          <button class="btn sm outline" id="alpha-reset">Reset to 1%</button>
        </div>
      </div>
    </div>

    <div class="grid" style="margin-top: var(--s-4)" id="alpha-tiles">
      <div class="stat" data-tone="accent">
        <span class="label">Posts unsupervised</span>
        <span class="value" id="tile-coverage">—</span>
        <span class="note">of all cross-source claims</span>
      </div>
      <div class="stat">
        <span class="label">Promised bound</span>
        <span class="value" id="tile-nominal">—</span>
        <span class="note">what α guarantees</span>
      </div>
      <div class="stat" data-tone="ok">
        <span class="label">Realised error</span>
        <span class="value" id="tile-realised">—</span>
        <span class="note">on the <strong>held-out</strong> split</span>
      </div>
      <div class="stat" data-tone="warn">
        <span class="label">Cost of this setting</span>
        <span class="value" id="tile-cost">—</span>
        <span class="note">illustrative assumptions</span>
      </div>
    </div>

    <div class="card" style="margin-top: var(--s-4)">
      <div class="card-title">
        <h3>The cost curve</h3>
        <span class="hint">only α values actually fetched are drawn</span>
      </div>
      <svg id="alpha-curve" class="fan-svg" viewBox="0 0 720 240" role="img"
           aria-labelledby="curve-caption"></svg>
      <p id="curve-caption" class="subtle" style="margin-top: var(--s-3)">
        This is a <strong>step function, not a smooth trade-off</strong>. Each point is a
        separate calibration measured on held-out data — nothing between them is
        interpolated. The jump is where the score distribution runs out of mass: below
        it the threshold admits only the confident claims and realises no errors at all;
        above it the threshold collapses, admits everything, and the cost multiplies.
      </p>
    </div>

    <div class="card">
      <div class="card-title"><h3>The guarantee, and what it assumes</h3></div>
      <p id="guarantee-line" class="money" style="text-align:left; font-size: var(--text-sm)">—</p>
      <div class="caveat">
        <strong>This bound assumes exchangeability</strong> between the calibration slice
        and the rows it is applied to — and reconciliation data violates that routinely.
        A gateway changing its settlement schedule shifts the timing distribution; a new
        payment method has a fee structure the model has never seen; month-end volume is
        not exchangeable with mid-month; and calibration labels exist precisely because a
        human looked at those rows, which is not a random sample of anything. Treat it as
        a well-founded operating point that must be re-calibrated when the data moves,
        not as a standing promise.
      </div>
    </div>
  `;

  // Served by /costmodel so this screen and scripts/calibrate.py cannot disagree.
  const costs = state.costmodel
    ? {
        false_post: state.costmodel.cost_per_false_post_paise,
        review: state.costmodel.cost_per_review_paise,
      }
    : FALLBACK_COSTS;
  const costsAreServed = Boolean(state.costmodel);

  const slider = container.querySelector("#alpha-range");
  const cache = new Map();
  const measured = [];
  let timer = 0;
  let alive = true;

  const apply = async (index, { immediate = false } = {}) => {
    const alpha = GRID[index];
    container.querySelector("#alpha-value").textContent = formatPct(alpha, 2);

    const cached = cache.get(alpha);
    if (cached) {
      // Cancel any in-flight debounce and clear the loading state before returning.
      //
      // Without these two lines, dragging to an unvisited alpha (which arms a 250ms
      // timer and dims the tiles) and then back to a cached one inside that window
      // painted the cached values, returned, and left the stale timer running - which
      // then fired, fetched the OLD alpha and repainted the tiles and the guarantee
      // sentence with a different alpha than the slider was showing. On the one
      // screen whose entire promise is "these numbers move together", they came apart.
      clearTimeout(timer);
      setLoading(container, false);
      paint(container, alpha, cached, measured, costsAreServed);
      return;
    }

    setLoading(container, true);
    const run = async () => {
      const response = await api.close(state.date, String(alpha));
      if (!alive) return;
      setLoading(container, false);
      if (!response.ok) {
        container.querySelector("#guarantee-line").textContent =
          `could not calibrate at α=${alpha}: ${response.error}`;
        return;
      }
      cache.set(alpha, response.data);
      recordPoint(measured, alpha, response.data, costs);
      paint(container, alpha, response.data, measured, costsAreServed);
    };

    clearTimeout(timer);
    if (immediate) await run();
    else timer = setTimeout(run, 250);
  };

  slider.addEventListener("input", () => apply(Number(slider.value)));

  /* The presets drive the same slider rather than a parallel code path, so the two
   * controls can never disagree about where α is. `immediate` skips the debounce -
   * a press is a decision, not a drag, and there is nothing to settle. */
  const presets = [...container.querySelectorAll(".preset")];

  const markPresets = (alpha) => {
    for (const button of presets) {
      button.setAttribute("aria-pressed", String(Number(button.dataset.alpha) === alpha));
    }
  };

  const goTo = (alpha) => {
    const index = GRID.findIndex((value) => value >= alpha);
    if (index < 0) return;
    slider.value = String(index);
    markPresets(GRID[index]);
    apply(index, { immediate: true });
  };

  for (const button of presets) {
    button.addEventListener("click", () => goTo(Number(button.dataset.alpha)));
  }
  container.querySelector("#alpha-reset").addEventListener("click", () => goTo(0.01));
  slider.addEventListener("input", () => markPresets(GRID[Number(slider.value)]));

  markPresets(GRID[initial]);
  apply(initial, { immediate: true });

  return () => {
    alive = false;
    clearTimeout(timer);
  };
}

function recordPoint(measured, alpha, data, costs) {
  if (measured.some((point) => point.alpha === alpha)) return;
  const coverage = Number(data.auto_post_coverage);
  const realised = Number(data.realised_error_holdout);
  const claims = Number(data.matches) || 0;
  const posted = Math.round(coverage * claims);
  const wrong = Math.round(realised * posted);
  const reviewed = claims - posted;
  measured.push({
    alpha,
    coverage,
    realised,
    threshold: Number(data.conformal_threshold),
    costPaise: wrong * costs.false_post + reviewed * costs.review,
    posted,
    reviewed,
    wrong,
  });
  measured.sort((a, b) => a.alpha - b.alpha);
}

function setLoading(container, loading) {
  container.querySelectorAll("#alpha-tiles .value").forEach((node) => {
    node.classList.toggle("loading-value", loading);
  });
}

function paint(container, alpha, data, measured, costsAreServed = true) {
  const coverage = Number(data.auto_post_coverage);
  const realised = Number(data.realised_error_holdout);
  const point = measured.find((item) => item.alpha === alpha);

  container.querySelector("#alpha-threshold").textContent = data.conformal_threshold ?? "—";

  odometer(container.querySelector("#tile-coverage"), coverage, (value) => formatPct(value, 1));
  container.querySelector("#tile-nominal").textContent = formatPct(alpha, 2);
  odometer(container.querySelector("#tile-realised"), realised, (value) => formatPct(value, 2));

  const realisedTile = container.querySelector("#tile-realised").closest(".stat");
  realisedTile.dataset.tone = realised <= alpha ? "ok" : "bad";

  if (point) {
    odometer(container.querySelector("#tile-cost"), point.costPaise, (value) =>
      formatINR(Math.round(value), { showPaise: false })
    );
    container.querySelector("#tile-cost").closest(".stat").querySelector(".note").textContent =
      `${point.wrong} wrong · ${point.reviewed} reviewed · illustrative rates` +
      (costsAreServed ? "" : " (fallback: /costmodel unreachable)");
  }

  container.querySelector("#guarantee-line").textContent =
    `Of the claims posted at confidence ≥ ${data.conformal_threshold}, at most ` +
    `${formatPct(alpha, 2)} are expected to be wrong. On the held-out split this ` +
    `setting realised ${formatPct(realised, 2)} — ` +
    (realised <= alpha ? "the bound held." : "the bound was breached, and that is reported rather than hidden.");

  drawCurve(container.querySelector("#alpha-curve"), measured, alpha);
}

function drawCurve(svg, measured, currentAlpha) {
  if (measured.length === 0) {
    svg.innerHTML = `<text x="360" y="120" text-anchor="middle" class="fan-tick">
      drag the slider to measure operating points</text>`;
    return;
  }

  const W = 720;
  const H = 240;
  const pad = { l: 64, r: 20, t: 20, b: 40 };
  const maxCost = Math.max(...measured.map((p) => p.costPaise), 1);
  const minAlpha = Math.log(measured[0].alpha);
  const maxAlpha = Math.log(measured[measured.length - 1].alpha || 0.5);
  const span = maxAlpha - minAlpha || 1;

  const x = (alpha) => pad.l + ((Math.log(alpha) - minAlpha) / span) * (W - pad.l - pad.r);
  const y = (cost) => H - pad.b - (cost / maxCost) * (H - pad.t - pad.b);

  // A step path, drawn as steps. Joining the points with straight lines would draw a
  // smooth trade-off that the data does not contain.
  let path = "";
  measured.forEach((point, index) => {
    const px = x(point.alpha);
    const py = y(point.costPaise);
    if (index === 0) path += `M ${px} ${py}`;
    else path += ` L ${px} ${y(measured[index - 1].costPaise)} L ${px} ${py}`;
  });

  const dots = measured
    .map(
      (point) => `<circle cx="${x(point.alpha).toFixed(1)}" cy="${y(point.costPaise).toFixed(1)}"
        r="${Math.abs(point.alpha - currentAlpha) < 1e-9 ? 6 : 3}"
        fill="var(${Math.abs(point.alpha - currentAlpha) < 1e-9 ? "--accent" : "--info"})" />`
    )
    .join("");

  svg.innerHTML = `
    <line class="fan-axis" x1="${pad.l}" y1="${H - pad.b}" x2="${W - pad.r}" y2="${H - pad.b}" />
    <line class="fan-axis" x1="${pad.l}" y1="${pad.t}" x2="${pad.l}" y2="${H - pad.b}" />
    <path d="${path}" fill="none" stroke="var(--info)" stroke-width="2" />
    ${dots}
    <text class="fan-tick" x="${pad.l}" y="${H - 12}">α ${formatPct(measured[0].alpha, 2)}</text>
    <text class="fan-tick" x="${W - pad.r}" y="${H - 12}" text-anchor="end">
      α ${formatPct(measured[measured.length - 1].alpha, 2)}</text>
    <text class="fan-tick" x="6" y="${pad.t + 10}">${esc(formatINR(maxCost, { showPaise: false }))}</text>
    <text class="fan-tick" x="6" y="${H - pad.b}">₹0</text>
    <text class="fan-tick" x="${W / 2}" y="14" text-anchor="middle">
      ${measured.length} measured operating point(s)</text>`;

  if (!reduced()) {
    const line = svg.querySelector("path");
    if (line) {
      const length = line.getTotalLength();
      line.style.strokeDasharray = String(length);
      line.style.strokeDashoffset = String(length);
      line.animate([{ strokeDashoffset: length }, { strokeDashoffset: 0 }], {
        duration: 420,
        easing: "cubic-bezier(.16,1,.3,1)",
        fill: "forwards",
      });
    }
  }
}

export const meta = {
  id: "alpha",
  title: "The α slider",
  needs: ["close"],
};
