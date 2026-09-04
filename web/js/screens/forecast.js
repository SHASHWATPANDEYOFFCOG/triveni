/* Screen 6 - The cash forecast, and the alert that fires on the lower bound.
 *
 * One picture, making one argument.
 *
 * Everybody alerts on the point forecast. A point forecast is a **median**, so alerting
 * on it means alerting once there is already a coin-flip chance of being short - too
 * late to move anything - and it stays silent in exactly the case that matters: Rs 6L
 * expected against a Rs 5L payroll looks comfortable until you notice the band runs
 * from Rs 2L to Rs 10L. So the fan chart here draws the point forecast as a thin dashed
 * median and gives the weight to the **lower bound**, and the alert marker is a bar
 * measured from the lower bound up to the commitment line. If you can see the picture
 * you can see the argument: the shortfall is a gap the median never touches.
 *
 * The band is **cumulative**, not per-day, and that is not a cosmetic choice. A payroll
 * on the 20th is met by everything that lands up to the 20th, so `cumulative_by()` in
 * forecast/alerts.py sums points and lower bounds to the due date - and this screen
 * runs the identical sum. That is why the alert dot lands *on* the curve rather than
 * near it: it is the same arithmetic, not a second opinion. A per-day chart would have
 * put a Rs 5,00,000 payroll line ten screens above a Rs 40,000 day and taught nothing.
 *
 * The forecast is fetched on a button press, never on mount. Two reasons, both real:
 * the shell hands screens their data and this data is not in it, and a six-fold
 * rolling-origin backtest over five models costs about ninety seconds - a page that
 * silently spends that on arrival is a page nobody profiles until the demo stalls.
 *
 * And on the shipped seed the honest answer is *no chart at all*: the backtest needs 74
 * days of history and the seed carries 21. That path renders the reason and the two
 * commands that fix it. A fan chart over a fabricated series would look exactly as
 * convincing as a real one, which is the whole problem with drawing it.
 */

import { esc, formatCompact, formatDate, formatINR, formatPct, humanise } from "../format.js";
import { drawPath, growBar, reduced, stagger } from "../motion.js";
import { api } from "../api.js";

/** Horizons the tool advertises. 14 is `DEFAULT_HORIZON` in forecast/service.py. */
const HORIZONS = [7, 14, 21, 30];
const DEFAULT_HORIZON = 14;

/* forecast/alerts.py grades an alert info / watch / warn / critical. app.css knows the
 * badge vocabulary high / medium / low / info. Neither list is wrong and neither is
 * going to change for the other, so the translation lives here, once, spelled out -
 * rather than as four `severity === "critical" ? ...` ternaries scattered down the file
 * that would silently colour an unknown grade as "fine". */
const SEVERITY = {
  critical: { badge: "high", word: "Critical", gloss: "even the point forecast falls short" },
  warn: { badge: "medium", word: "Warn", gloss: "the lower bound falls short" },
  watch: { badge: "low", word: "Watch", gloss: "the cushion is thin - nothing to do yet" },
  info: { badge: "info", word: "Info", gloss: "recorded, not a risk" },
};
const UNKNOWN_SEVERITY = { badge: "info", word: "Unrecognised", gloss: "grade not in the taxonomy" };

/* Chart geometry, in viewBox user units. The SVG scales between a 34rem floor (below
 * which its wrapper scrolls rather than the page) and a 56rem ceiling, so the 15-unit
 * axis type renders somewhere between about 9.5px and 15px - small at the bottom of
 * that range, but the same numbers are in the table underneath, which is the copy a
 * screen reader and a squinting judge both get. */
const CHART = { w: 900, h: 340, top: 24, right: 28, bottom: 46, left: 100 };

export function render(container, state, helpers = {}) {
  ensureStyles();

  const offline = Boolean(state?.error);

  container.innerHTML = `
    <div class="screen-head">
      <div class="eyebrow">Loop 3 · Forecast</div>
      <h1>The cash that will actually land</h1>
      <p>
        Built from <strong>reconciled bank credits</strong>, not gateway captures.
        Captures tell you what was sold; only the bank side tells you what arrived, and
        payroll is paid out of the second one. The band below is conformal, its realised
        coverage is printed beside its nominal level, and the alert fires on the
        <strong>lower bound</strong>.
      </p>
    </div>

    <div class="card fc-controls">
      <div class="row">
        <div class="fc-field">
          <label for="fc-horizon">Horizon</label>
          <select id="fc-horizon" ${offline ? "disabled" : ""}>
            ${HORIZONS.map(
              (days) =>
                `<option value="${days}" ${days === DEFAULT_HORIZON ? "selected" : ""}>${days} days</option>`
            ).join("")}
          </select>
        </div>
        <button class="btn primary" id="fc-run" ${offline ? "disabled" : ""}>
          <span aria-hidden="true">▶</span> Run the forecast
        </button>
        <p class="fc-status subtle" id="fc-status" aria-live="polite">
          ${
            offline
              ? "The Triveni API is not answering, so there is nothing to ask."
              : "Five models, six rolling-origin folds. Roughly ninety seconds."
          }
        </p>
      </div>
    </div>

    <div id="fc-body">${offline ? paintUnreachable(state.error) : paintPrimed()}</div>
  `;

  const body = container.querySelector("#fc-body");
  const status = container.querySelector("#fc-status");
  const button = container.querySelector("#fc-run");
  const horizon = container.querySelector("#fc-horizon");

  /* A screen can be navigated away from mid-request, and a ninety-second request
   * outlives a lot of navigation. The flag is checked before every paint so a late
   * response cannot write into a container the shell has already handed to another
   * screen - the class of bug that shows one screen's numbers under another's title. */
  let live = true;

  button?.addEventListener("click", async () => {
    const days = Number(horizon.value) || DEFAULT_HORIZON;
    button.disabled = true;
    horizon.disabled = true;
    button.innerHTML = '<span aria-hidden="true">◌</span> Backtesting…';
    say(status, `Re-fitting five models over ${days} days. This takes about ninety seconds.`);
    body.setAttribute("aria-busy", "true");
    body.innerHTML = paintPending();

    const result = await api.mcpCall("forecast_cash", { horizon_days: days });
    if (!live) return;

    body.removeAttribute("aria-busy");
    button.disabled = false;
    horizon.disabled = false;
    button.innerHTML = '<span aria-hidden="true">↻</span> Run again';

    paintResult(body, status, result, helpers);
  });

  return () => {
    live = false;
  };
}

/** Announce to the local status line and to the shell's page-level live region. */
function say(status, message) {
  if (status) status.textContent = message;
  const live = document.getElementById("live");
  if (live) live.textContent = message;
}

/* --------------------------------------------------------------------------- */
/* The three ways this ends                                                     */
/* --------------------------------------------------------------------------- */
function paintResult(body, status, result, helpers) {
  // Two different "not ok"s, and conflating them would be a lie in one direction or
  // the other. `result.ok` is the transport: did the API answer at all. `data.ok` is
  // the tool's own verdict: it answered, and its answer is "I cannot forecast this".
  if (!result.ok) {
    body.innerHTML = paintUnreachable(result.error);
    say(status, "The forecast could not be requested.");
    return;
  }
  const data = result.data ?? {};
  if (data.ok !== true || data.available === false) {
    body.innerHTML = paintUnavailable(data.reason);
    say(status, "The forecast is unavailable: there is not enough history to back it.");
    return;
  }

  const built = paintForecast(data, helpers);
  body.innerHTML = built.html;
  if (built.mount) built.mount(body);

  const alerts = Array.isArray(data.alerts) ? data.alerts.length : 0;
  say(
    status,
    `${humanise(data.model)} won the backtest. ${data.horizon_days} days, expected ` +
      `${data.expected}, confident ${data.confident_lower_bound}, ` +
      `${alerts === 0 ? "no liquidity alerts" : `${alerts} liquidity alert(s) on the lower bound`}.`
  );
}

/** Before the first press. An empty state that says what will appear and what it costs. */
function paintPrimed() {
  return `
    <div class="empty">
      <h3>The forecast has not been run yet</h3>
      <p>
        A fan chart of cash landing per day, a conformal band with its realised coverage
        printed beside its nominal level, the five-model backtest ladder, and any
        liquidity alert where the lower bound falls under a committed outflow.
      </p>
      <p class="subtle">
        Press <strong>Run the forecast</strong> above, or compute it once and cache it:
      </p>
      <code>python -m scripts.forecast_report</code>
    </div>`;
}

/** Shaped like the content it replaces - the chart block, then the ladder. Never a spinner. */
function paintPending() {
  return `
    <div class="card">
      <div class="skeleton" style="height: 1rem; width: 14rem; margin-bottom: var(--s-4)"></div>
      <div class="skeleton" style="height: 17rem"></div>
    </div>
    <div class="card">
      <div class="skeleton" style="height: 9rem"></div>
    </div>`;
}

/** The tool answered honestly that it cannot answer. Teach the way out of it. */
function paintUnavailable(reason) {
  return `
    <div class="empty">
      <h3>There is not enough history to forecast</h3>
      ${reason ? `<p>${esc(reason)}</p>` : ""}
      <p class="subtle">
        A 14-day horizon is scored over six rolling-origin folds, and that needs
        74 days of series before the first fold has anywhere to stand. Generate the long
        history, then compute the report:
      </p>
      <code>python -m data.gen --spec history</code>
      <code>python -m scripts.forecast_report</code>
      <p class="subtle" style="margin-top: var(--s-4)">
        Nothing is drawn in the meantime. A fan chart over a fabricated series is
        indistinguishable from a real one, which is precisely why there is not one here.
      </p>
    </div>`;
}

/** The API never answered. Different problem, different command. */
function paintUnreachable(error) {
  return `
    <div class="empty">
      <h3>The forecast could not be requested</h3>
      <p>${esc(error || "the call to forecast_cash did not complete")}</p>
      <p class="subtle">The tool is served by the Triveni API. Start it with:</p>
      <code>make run</code>
    </div>`;
}

/* --------------------------------------------------------------------------- */
/* The forecast itself                                                          */
/* --------------------------------------------------------------------------- */
function paintForecast(data, helpers) {
  const intervals = Array.isArray(data.intervals) ? data.intervals : [];
  const alerts = Array.isArray(data.alerts) ? data.alerts : [];
  const coverage = data.coverage ?? null;
  const chart = buildChart(intervals, alerts, coverage);

  const html = `
    ${paintTiles(data, coverage, chart)}
    ${
      chart
        ? `<div class="card fc-chart-card">
             <div class="card-title">
               <h3>Cumulative cash landing, ${esc(String(data.horizon_days ?? intervals.length))} days</h3>
               <span class="hint">forecast by ${esc(humanise(data.model))}</span>
             </div>
             <div class="scroll-x fc-chart-wrap">${chart.svg}</div>
             ${chart.legend}
             <p class="subtle fc-note">
               Cumulative, because a commitment on a date is met by everything that lands
               up to that date. Summing lower bounds is deliberately conservative - the
               true joint lower bound of a sum is tighter than the sum of the marginals,
               and for a solvency warning that is the right direction to be wrong in.
             </p>
           </div>`
        : `<div class="empty">
             <h3>The forecast came back without a usable series</h3>
             <p>Every figure this chart draws is integer paise from
             <code>intervals</code>, and this payload did not carry it. Rather than
             plotting something shaped like a forecast, there is nothing here.</p>
           </div>`
    }
    ${paintCoverage(coverage, data, helpers)}
    ${paintAlerts(alerts, coverage)}
    ${paintLadder(data)}
    ${chart ? paintTable(chart.rows) : ""}
    ${data.reason ? `<p class="subtle fc-reason">${esc(data.reason)}</p>` : ""}
  `;

  const mount = (root) => {
    stagger([...root.querySelectorAll(".fc-tile")], { step: 50 });
    stagger([...root.querySelectorAll(".fc-alert")], { step: 60 });
    animateChart(root);
    const jump = root.querySelector("#fc-see-alpha");
    if (jump && helpers.showScreen) {
      jump.addEventListener("click", () => helpers.showScreen("alpha"));
    }
  };

  return { html, mount };
}

function paintTiles(data, coverage, chart) {
  const first = chart?.rows?.[0];
  const holds = coverage?.holds === true;
  /* `expected` and `confident_lower_bound` arrive already formatted by core.money -
   * the same formatter the CLI and the audit log use. They go in a .money verbatim.
   * Re-parsing a formatted string back into a float to re-format it is how two
   * surfaces of the same product start disagreeing about a rupee. */
  const tiles = [
    {
      label: "Expected",
      value: data.expected,
      money: true,
      tone: "",
      note: "the point forecast - a median",
    },
    {
      label: "Confident lower bound",
      value: data.confident_lower_bound,
      money: true,
      tone: "accent",
      note: "the figure every alert uses",
    },
    {
      label: "Model",
      value: humanise(data.model),
      tone: "",
      note: "won a rolling-origin backtest",
    },
    {
      label: "Realised coverage",
      value: coverage ? formatPct(coverage.realised, 1) : "—",
      tone: holds ? "ok" : "warn",
      note: coverage ? `nominal ${formatPct(coverage.nominal, 0)}` : "not reported",
    },
    {
      label: "Horizon",
      value: `${data.horizon_days ?? "—"} days`,
      tone: "",
      note: first ? `from ${formatDate(first.date)}` : "",
    },
  ];
  return `<div class="grid fc-tiles">
    ${tiles
      .map(
        (tile) => `
      <div class="stat fc-tile" data-tone="${tile.tone}">
        <span class="label">${esc(tile.label)}</span>
        <span class="value${tile.money ? " money" : ""}">${esc(String(tile.value ?? "—"))}</span>
        ${tile.note ? `<span class="note">${esc(tile.note)}</span>` : ""}
      </div>`
      )
      .join("")}
  </div>`;
}

/* The coverage panel exists to make one number impossible to miss. A 90% band that
 * contains the truth 88.1% of the time is an 88.1% band; the only way anybody finds
 * out is if the realised figure is printed next to the nominal one, every time, even -
 * especially - when it is the worse of the two. */
function paintCoverage(coverage, data, helpers) {
  if (!coverage) {
    return `
      <div class="card">
        <div class="empty">
          <h3>This band reported no coverage</h3>
          <p>A nominal level with no realised level beside it is a claim, not a
          measurement, so nothing is shown for it.</p>
        </div>
      </div>`;
  }
  const holds = coverage.holds === true;
  return `
    <div class="card fc-coverage">
      <div class="card-title">
        <h3>Did the band actually cover?</h3>
        <span class="badge" data-sev="${holds ? "low" : "medium"}">
          nominal ${esc(formatPct(coverage.nominal, 0))} · realised ${esc(formatPct(coverage.realised, 1))}
        </span>
      </div>
      <p>
        ${
          holds
            ? `The interval held: it contained the truth ${esc(formatPct(coverage.realised, 1))} of
               the time against a nominal ${esc(formatPct(coverage.nominal, 0))}. The guarantee is
               distribution-free under exchangeability, and the residuals are calibrated on a
               recent window rather than all of history, because tomorrow is not exchangeable
               with last March.`
            : `<strong>It did not hold.</strong> The band was sold as
               ${esc(formatPct(coverage.nominal, 0))} and covered
               ${esc(formatPct(coverage.realised, 1))} of the time, which makes it a
               ${esc(formatPct(coverage.realised, 1))} band. That gap is printed rather than
               tuned away: a treasurer who is told 90% and given 88% has been given the wrong
               number, and the only defence is that the right one is on the screen next to it.`
        }
      </p>
      <div class="row">
        <button class="btn ghost" id="fc-see-alpha" type="button">
          Where this guarantee comes from →
        </button>
        <span class="subtle">${esc(humanise(data.model))} · the same conformal machinery as the close</span>
      </div>
    </div>`;
}

/* --------------------------------------------------------------------------- */
/* The fan chart                                                                */
/* --------------------------------------------------------------------------- */

/** Integer paise or nothing. A silent `NaN` becomes a plausible-looking line. */
function paise(value) {
  const number = typeof value === "string" ? Number.parseInt(value, 10) : Number(value);
  return Number.isFinite(number) ? Math.round(number) : null;
}

/**
 * Sum points, lowers and uppers forward - the same walk `cumulative_by()` does in
 * forecast/alerts.py, which is why an alert's own `confident_paise` lands exactly on
 * the lower-bound curve at its due date instead of somewhere near it.
 */
function cumulate(intervals) {
  let point = 0;
  let lower = 0;
  let upper = 0;
  const rows = [];
  for (const interval of intervals) {
    const p = paise(interval.point_paise);
    const l = paise(interval.lower_paise);
    const u = paise(interval.upper_paise);
    if (p === null || l === null || u === null) return null;
    point += p;
    lower += l;
    upper += u;
    rows.push({ date: interval.date, day: { point: p, lower: l, upper: u }, point, lower, upper });
  }
  return rows.length ? rows : null;
}

function buildChart(intervals, alerts, coverage) {
  const rows = cumulate(intervals);
  if (!rows) return null;

  /* An alert is only drawable if its due date is a day we actually forecast. One that
   * falls outside the horizon still gets its card below - it is simply not on a chart
   * that does not reach that far, and pinning it to the last column would put a
   * shortfall on a date the model never predicted. */
  const marks = alerts
    .map((alert) => ({
      alert,
      index: rows.findIndex((row) => String(row.date) === String(alert.due_on)),
      amount: paise(alert.commitment?.amount_paise),
      expected: paise(alert.expected_paise),
      confident: paise(alert.confident_paise),
      shortfall: paise(alert.shortfall_paise) ?? 0,
      severity: String(alert.severity ?? "info"),
    }))
    .filter((mark) => mark.index >= 0 && mark.amount !== null && mark.confident !== null);

  const span = CHART.w - CHART.left - CHART.right;
  const height = CHART.h - CHART.top - CHART.bottom;
  const values = [0];
  rows.forEach((row) => values.push(row.upper, row.lower, row.point));
  marks.forEach((mark) => values.push(mark.amount, mark.confident, mark.expected ?? mark.confident));
  const low = Math.min(...values);
  const high = Math.max(...values);
  const spread = high - low || 1;

  const x = (index) => CHART.left + (span * index) / Math.max(rows.length - 1, 1);
  const y = (value) => CHART.top + height * (1 - (value - low) / spread);

  const line = (pick) => rows.map((row, index) => `${x(index)},${y(pick(row))}`).join(" L ");
  // Out along the upper edge, back along the lower one. Each point uses its OWN
  // x-position and the *order* is reversed exactly once to close the path.
  //
  // This previously mapped row i to x(n-1-i) and then called .reverse().reverse(),
  // which is the identity - so row 0's lower bound was plotted in the last column
  // and row n-1's in the first. Because the cumulative lower bound rises
  // monotonically, the drawn floor fell while the ceiling rose and the "band" was a
  // crossed wedge with the lower-bound line sitting outside it. On the one screen
  // whose entire argument is "the alert measures from the lower bound", that made
  // the shaded region misrepresent the data.
  const upperEdge = rows.map((row, index) => `${x(index)},${y(row.upper)}`);
  const lowerEdge = rows
    .map((row, index) => `${x(index)},${y(row.lower)}`)
    .reverse();
  const band = `M ${upperEdge.join(" L ")} L ${lowerEdge.join(" L ")} Z`;

  const ticks = Array.from({ length: 5 }, (_, step) => low + (spread * step) / 4);
  // With 14 columns every label collides; label roughly seven of them and let the
  // table below carry the rest.
  const every = Math.max(1, Math.ceil(rows.length / 7));
  const sameYear =
    String(rows[0].date).slice(0, 4) === String(rows[rows.length - 1].date).slice(0, 4);

  const bandFloor = CHART.top + height;
  const level = coverage ? formatPct(coverage.nominal, 0) : "";

  const svg = `
    <svg class="fc-svg" viewBox="0 0 ${CHART.w} ${CHART.h}" role="img"
         aria-labelledby="fc-svg-title fc-svg-desc">
      <title id="fc-svg-title">Cumulative cash forecast against committed outflows</title>
      <desc id="fc-svg-desc">${esc(altText(rows, marks, coverage))}</desc>

      <g class="fc-grid">
        ${ticks
          .map(
            (value) => `
          <line x1="${CHART.left}" y1="${y(value)}" x2="${CHART.w - CHART.right}" y2="${y(value)}"></line>
          <text class="fc-axis money" x="${CHART.left - 10}" y="${y(value) + 5}" text-anchor="end"
            >${esc(formatCompact(value))}</text>`
          )
          .join("")}
      </g>

      <g class="fc-dates">
        ${rows
          .map((row, index) =>
            index % every === 0 || index === rows.length - 1
              ? `<text class="fc-axis" x="${x(index)}" y="${CHART.h - CHART.bottom + 26}"
                   text-anchor="middle">${esc(axisDate(row.date, sameYear))}</text>`
              : ""
          )
          .join("")}
      </g>

      <!-- The band grows out of the baseline; the origin has to be the baseline in
           user units, not the element's own box, or it inflates from its middle. -->
      <g class="fc-band-group" style="transform-origin: 0px ${bandFloor}px">
        <path class="fc-band" d="${band}"></path>
      </g>

      <path class="fc-point-line" d="M ${line((row) => row.point)}" fill="none"></path>
      <path class="fc-lower-line" d="M ${line((row) => row.lower)}" fill="none"></path>

      <g class="fc-annotations">
        ${marks.map((mark) => commitmentMarkup(mark, x, y, level)).join("")}
      </g>
    </svg>`;

  return { svg, rows, legend: legendMarkup(level, marks.length) };
}

/**
 * One commitment: the line it must clear, and the bar showing what falls short of it.
 *
 * The bar is drawn from the LOWER BOUND up to the commitment - never from the point
 * forecast - and the median is left as a hollow ring, usually sitting comfortably above
 * the line it is not being measured against. That contrast is the entire screen.
 */
function commitmentMarkup(mark, x, y, level) {
  const grade = SEVERITY[mark.severity] ?? UNKNOWN_SEVERITY;
  const at = x(mark.index);
  const yCommit = y(mark.amount);
  const yConfident = y(mark.confident);
  const short = mark.shortfall > 0;
  const label = mark.alert.commitment?.label ?? mark.alert.commitment?.kind ?? "commitment";

  return `
    <g class="fc-mark" data-sev="${esc(mark.severity)}">
      <line class="fc-commit" x1="${CHART.left}" y1="${yCommit}"
            x2="${CHART.w - CHART.right}" y2="${yCommit}"></line>
      <text class="fc-commit-label" x="${CHART.left + 6}" y="${yCommit - 8}"
        >${esc(label)} · <tspan class="money">${esc(formatINR(mark.amount))}</tspan> due ${esc(
          formatDate(mark.alert.due_on)
        )}</text>
      ${
        short
          ? `<line class="fc-gap" x1="${at}" y1="${yConfident}" x2="${at}" y2="${yCommit}"></line>
             <text class="fc-gap-label money" x="${at + 8}" y="${(yConfident + yCommit) / 2 + 4}"
               >short ${esc(formatINR(mark.shortfall))}</text>`
          : ""
      }
      ${
        mark.expected !== null
          ? `<circle class="fc-median-dot" cx="${at}" cy="${y(mark.expected)}" r="5"></circle>`
          : ""
      }
      <circle class="fc-dot" cx="${at}" cy="${yConfident}" r="5.5"></circle>
      <text class="fc-dot-label" x="${at + 10}" y="${yConfident + 20}"
        >${esc(level ? `${level} lower bound` : "lower bound")}</text>
    </g>`;
}

function legendMarkup(level, marked) {
  const items = [
    ["fc-key-band", `the ${level || "conformal"} band, cumulative`],
    ["fc-key-lower", `${level || "conformal"} lower bound — what the alert reads`],
    ["fc-key-point", "point forecast — a median, and not what the alert reads"],
    ["fc-key-commit", "a committed outflow, on its due date"],
  ];
  return `
    <div class="fc-legend">
      ${items
        .map(([key, text]) => `<span class="fc-legend-item"><i class="${key}"></i>${esc(text)}</span>`)
        .join("")}
      ${
        marked
          ? `<span class="fc-legend-item fc-legend-note">the bar is measured from the lower
             bound up to the line, not from the median</span>`
          : ""
      }
    </div>`;
}

function axisDate(iso, sameYear) {
  const full = formatDate(iso);
  // Over a fortnight the year is the same on every column, so repeating it fourteen
  // times is noise - unless the horizon crosses a new year, when it is the whole point.
  return sameYear ? full.replace(/\s\d{4}$/, "") : full;
}

function altText(rows, marks, coverage) {
  const last = rows[rows.length - 1];
  const level = coverage ? formatPct(coverage.nominal, 0) : "the nominal";
  const head =
    `Over ${rows.length} days the point forecast accumulates to ${formatINR(last.point)}, ` +
    `and the ${level} lower bound to ${formatINR(last.lower)}.`;
  if (!marks.length) return `${head} No committed outflow falls outside the band.`;
  return `${head} ${marks
    .map((mark) => {
      const label = mark.alert.commitment?.label ?? "a commitment";
      return (
        `${label} of ${formatINR(mark.amount)} falls due ${formatDate(mark.alert.due_on)}, by ` +
        `which date the lower bound is ${formatINR(mark.confident)}` +
        (mark.shortfall > 0
          ? `, ${formatINR(mark.shortfall)} short, while the point forecast is ` +
            `${formatINR(mark.expected ?? mark.confident)}.`
          : `, which clears it.`)
      );
    })
    .join(" ")}`;
}

/** Band grows, lines draw, annotations land. Every step is a motion.js no-op when asked. */
function animateChart(root) {
  const band = root.querySelector(".fc-band-group");
  if (band) growBar(band, "scaleY", 520);

  const lower = root.querySelector(".fc-lower-line");
  const point = root.querySelector(".fc-point-line");
  if (lower) drawPath(lower, 760);
  if (point) drawPath(point, 760);

  const annotations = root.querySelector(".fc-annotations");
  // The one hand-rolled animation on this screen, so it carries the guard itself: the
  // alert should arrive *after* the curve it is reading, and a delayed fade under
  // reduced motion would leave the shortfall invisible for half a second.
  if (annotations && !reduced()) {
    annotations.animate([{ opacity: 0 }, { opacity: 1 }], {
      duration: 320,
      delay: 560,
      easing: "cubic-bezier(.16,1,.3,1)",
      fill: "backwards",
    });
  }
}

/* --------------------------------------------------------------------------- */
/* Alerts                                                                       */
/* --------------------------------------------------------------------------- */
function paintAlerts(alerts, coverage) {
  const level = coverage ? formatPct(coverage.nominal, 0) : "the conformal";
  const disclaimer = `
    <p class="subtle fc-proposes">
      <strong>Triveni proposes; it never moves money.</strong> Every line above is a
      sentence for a human. There is no path from an alert to a transfer, because there
      is no transfer capability anywhere in the system for one to reach.
    </p>`;

  if (!alerts.length) {
    return `
      <div class="card">
        <div class="card-title">
          <h3>Liquidity alerts</h3>
          <span class="badge" data-state="matched">none fired</span>
        </div>
        <p>
          The ${esc(level)} lower bound clears every commitment in the horizon. Not "the
          forecast looks fine" - the conservative figure clears them, which is a
          different and much stronger statement.
        </p>
        ${disclaimer}
      </div>`;
  }

  return `
    <div class="card">
      <div class="card-title">
        <h3>Liquidity alerts</h3>
        <span class="hint">fired on the lower bound, not the point forecast</span>
      </div>
      <div class="stack">
        ${alerts.map((alert) => alertCard(alert, level)).join("")}
      </div>
      ${disclaimer}
    </div>`;
}

function alertCard(alert, level) {
  const grade = SEVERITY[String(alert.severity)] ?? UNKNOWN_SEVERITY;
  const commitment = alert.commitment ?? {};
  const amount = paise(commitment.amount_paise);
  const expected = paise(alert.expected_paise);
  const confident = paise(alert.confident_paise);
  const shortfall = paise(alert.shortfall_paise);

  // Figures are rendered only where the payload carried an integer. An em dash is an
  // honest "this did not arrive"; a zero would be a claim that it did.
  const figure = (label, value, note, extra = "") => `
    <div class="fc-figure ${extra}">
      <span class="fc-figure-label">${esc(label)}</span>
      <span class="money">${value === null ? "—" : esc(formatINR(value))}</span>
      ${note ? `<span class="fc-figure-note">${esc(note)}</span>` : ""}
    </div>`;

  return `
    <article class="fc-alert" data-sev="${esc(String(alert.severity ?? "info"))}">
      <div class="fc-alert-head">
        <span class="badge" data-sev="${grade.badge}">${esc(grade.word)}</span>
        <strong>${esc(commitment.label ?? "Commitment")}</strong>
        <span class="subtle">${esc(humanise(commitment.kind))} · due ${esc(formatDate(alert.due_on))}</span>
      </div>
      <p class="fc-alert-gloss subtle">${esc(grade.gloss)}</p>
      <div class="fc-figures">
        ${figure("Committed", amount, "must go out on the day")}
        ${figure("Confident by then", confident, `the ${level} lower bound`, "fc-figure-key")}
        ${figure("Expected by then", expected, "the median — not the trigger")}
        ${figure("Shortfall", shortfall, "against the lower bound")}
      </div>
      <p class="fc-alert-reason">${esc(alert.reason ?? "")}</p>
      ${
        alert.proposal
          ? `<p class="fc-alert-proposal"><span class="fc-proposal-tag">Proposal</span>${esc(
              alert.proposal
            )}</p>`
          : ""
      }
    </article>`;
}

/* --------------------------------------------------------------------------- */
/* The backtest ladder                                                          */
/* --------------------------------------------------------------------------- */
function paintLadder(data) {
  const scores = Array.isArray(data.backtest) ? data.backtest : [];
  if (!scores.length) {
    return `
      <div class="card">
        <div class="empty">
          <h3>No backtest came back with this forecast</h3>
          <p>A model with no ladder underneath it is an assertion. Regenerate the
          report to score the five rungs on identical folds.</p>
          <code>python -m scripts.forecast_report</code>
        </div>
      </div>`;
  }

  /* The verdict is computed against the baseline's OWN score, never against 1.0.
   * MASE scales by the *in-sample* seasonal-naive error, so an out-of-sample naive
   * forecast does not land on exactly 1.0 - on this series it lands at 0.837. Judging
   * everything against the constant is the bug that once labelled seasonal_drift
   * (0.927) a winner when it is materially worse than the baseline it exists to beat.
   * If the payload has no baseline row, there is no verdict to give and the cell says
   * so rather than guessing one. */
  const baseline = scores.find((score) => score.name === "seasonal_naive");
  const naiveMase = baseline ? Number(baseline.mase) : NaN;
  const folds = scores[0]?.folds;

  const verdict = (score) => {
    if (score.name === "seasonal_naive") return `<span class="badge" data-sev="info">baseline</span>`;
    const value = Number(score.mase);
    if (!Number.isFinite(value) || !Number.isFinite(naiveMase)) {
      return `<span class="subtle">no baseline to compare against</span>`;
    }
    return value < naiveMase
      ? `<span class="badge" data-state="matched">beats naive</span>`
      : `<span class="badge" data-sev="high">LOSES to naive</span>`;
  };

  const number = (value, places = 3) => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed.toFixed(places) : "—";
  };

  // Pinball loss is denominated in the series' own units - paise - so it is money and
  // is formatted as money. It scores the *band* rather than the middle, which is the
  // metric a liquidity decision actually turns on.
  const money = (value) => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? formatINR(Math.round(parsed)) : "—";
  };

  return `
    <div class="card">
      <div class="card-title">
        <h3>The ladder</h3>
        <span class="hint">
          ${folds ? `${esc(String(folds))} rolling-origin folds, ` : ""}identical for every rung
        </span>
      </div>
      <div class="scroll-x">
        <table>
          <thead>
            <tr>
              <th scope="col">Model</th>
              <th scope="col" class="num">MASE</th>
              <th scope="col" class="num">Pinball @ 05</th>
              <th scope="col" class="num">Pinball @ 50</th>
              <th scope="col" class="num">Pinball @ 95</th>
              <th scope="col">Verdict</th>
            </tr>
          </thead>
          <tbody>
            ${scores
              .map(
                (score) => `
              <tr ${score.name === data.model ? 'class="fc-winner"' : ""}>
                <th scope="row">
                  ${esc(humanise(score.name))}
                  ${
                    score.name === data.model
                      ? '<span class="badge" data-state="auto-posted">won</span>'
                      : ""
                  }
                </th>
                <td class="num money">${esc(number(score.mase))}</td>
                <td class="num money">${esc(money(score.pinball_05))}</td>
                <td class="num money">${esc(money(score.pinball_50))}</td>
                <td class="num money">${esc(money(score.pinball_95))}</td>
                <td>${verdict(score)}</td>
              </tr>`
              )
              .join("")}
          </tbody>
        </table>
      </div>
      <details class="disclosure fc-note">
        <summary>Why MASE and pinball, never MAPE</summary>
        <div class="body">
          MAPE is undefined on a zero-cash day and this series is full of structural
          zeros — every Sunday, every second and fourth Saturday. A metric that cannot
          be computed on a third of the series is not a metric. A rung that loses to
          the one-line baseline is published as losing; a ladder where the fanciest
          model always wins is a ladder nobody should believe.
        </div>
      </details>
    </div>`;
}

/* --------------------------------------------------------------------------- */
/* The chart, as numbers                                                        */
/* --------------------------------------------------------------------------- */
/* Every picture in Triveni has its table. The chart is the argument; this is the
 * evidence, and it is what a screen reader, a spreadsheet and a sceptic all get. */
function paintTable(rows) {
  return `
    <details class="card fc-details">
      <summary>The ${rows.length} days, as numbers</summary>
      <div class="scroll-x" style="margin-top: var(--s-4)">
        <table>
          <thead>
            <tr>
              <th scope="col">Date</th>
              <th scope="col" class="num">Point, that day</th>
              <th scope="col" class="num">Lower</th>
              <th scope="col" class="num">Upper</th>
              <th scope="col" class="num">Cumulative point</th>
              <th scope="col" class="num">Cumulative lower</th>
            </tr>
          </thead>
          <tbody>
            ${rows
              .map(
                (row) => `
              <tr>
                <th scope="row">${esc(formatDate(row.date))}</th>
                <td class="num money">${esc(formatINR(row.day.point))}</td>
                <td class="num money">${esc(formatINR(row.day.lower))}</td>
                <td class="num money">${esc(formatINR(row.day.upper))}</td>
                <td class="num money">${esc(formatINR(row.point))}</td>
                <td class="num money">${esc(formatINR(row.lower))}</td>
              </tr>`
              )
              .join("")}
          </tbody>
        </table>
      </div>
    </details>`;
}

/* --------------------------------------------------------------------------- */
/* Styles                                                                       */
/* --------------------------------------------------------------------------- */
/* A fan chart has no equivalent in app.css and inventing four new global primitives
 * for one screen would be worse than this. Injected into <head> once rather than into
 * the section, so re-rendering the screen does not re-parse a stylesheet, and every
 * declaration is a token - the severity colours arrive through one custom property so
 * a grade is coloured in exactly one place, whether it is a chart bar or a card edge. */
const STYLE = `
.fc-controls .row { align-items: center; }
.fc-field { display: flex; align-items: center; gap: var(--s-2); font-size: var(--text-sm); }
.fc-field label { color: var(--fg-muted); }
.fc-field select {
  font: inherit;
  font-size: var(--text-sm);
  color: var(--fg);
  background: var(--bg-raised);
  border: 1px solid var(--rule-strong);
  border-radius: var(--r-2);
  padding: var(--s-1) var(--s-2);
}
.fc-status { margin: 0; }
.fc-tiles { margin-bottom: var(--s-4); }
.fc-note { margin: var(--s-3) 0 0; max-width: 68ch; }
.fc-reason { margin-top: var(--s-4); max-width: 78ch; }

.fc-chart-wrap { padding-bottom: var(--s-2); }
.fc-svg { display: block; width: 100%; min-width: 34rem; max-width: 56rem; height: auto; margin-inline: auto; }
.fc-svg text { font-family: var(--font-ui); }
.fc-axis { font-size: 15px; fill: var(--fg-subtle); }
.fc-axis.money { font-family: var(--font-mono); }
.fc-grid line { stroke: var(--rule); stroke-width: 1; }
.fc-band { fill: color-mix(in srgb, var(--info) 18%, transparent); }
.fc-point-line {
  stroke: var(--fg-muted);
  stroke-width: 2;
  stroke-dasharray: 6 5;
  stroke-linecap: round;
}
.fc-lower-line { stroke: var(--accent); stroke-width: 3.5; stroke-linejoin: round; stroke-linecap: round; }

.fc-mark { --fc-sev: var(--fg-muted); }
.fc-mark[data-sev="critical"] { --fc-sev: var(--bad); }
.fc-mark[data-sev="warn"] { --fc-sev: var(--warn); }
.fc-mark[data-sev="watch"] { --fc-sev: var(--info); }
.fc-commit { stroke: var(--fc-sev); stroke-width: 2; stroke-dasharray: 3 4; }
.fc-commit-label { font-size: 15px; font-weight: 600; fill: var(--fc-sev); }
.fc-commit-label .money { font-family: var(--font-mono); }
.fc-gap { stroke: var(--fc-sev); stroke-width: 7; stroke-linecap: butt; opacity: 0.55; }
.fc-gap-label { font-size: 15px; font-weight: 600; fill: var(--fc-sev); font-family: var(--font-mono); }
.fc-dot { fill: var(--fc-sev); stroke: var(--bg-raised); stroke-width: 2; }
.fc-median-dot { fill: none; stroke: var(--fg-muted); stroke-width: 2; stroke-dasharray: 3 3; }
.fc-dot-label { font-size: 14px; fill: var(--fg-muted); }

.fc-legend {
  display: flex;
  flex-wrap: wrap;
  gap: var(--s-2) var(--s-4);
  margin-top: var(--s-3);
  font-size: var(--text-xs);
  color: var(--fg-muted);
}
.fc-legend-item { display: inline-flex; align-items: center; gap: var(--s-2); }
.fc-legend-item i { width: 1.25rem; height: 0; border-top-width: 3px; border-top-style: solid; flex: none; }
.fc-key-band { border-top-color: color-mix(in srgb, var(--info) 45%, transparent); border-top-width: 10px !important; }
.fc-key-lower { border-top-color: var(--accent); }
.fc-key-point { border-top-color: var(--fg-muted); border-top-style: dashed !important; }
.fc-key-commit { border-top-color: var(--bad); border-top-style: dotted !important; }
.fc-legend-note { color: var(--fg-subtle); font-style: italic; }

.fc-alert {
  border: 1px solid var(--rule);
  border-left: 3px solid var(--fc-sev, var(--fg-muted));
  border-radius: var(--r-2);
  padding: var(--s-3) var(--s-4);
  background: var(--bg-sunken);
}
.fc-alert[data-sev="critical"] { --fc-sev: var(--bad); }
.fc-alert[data-sev="warn"] { --fc-sev: var(--warn); }
.fc-alert[data-sev="watch"] { --fc-sev: var(--info); }
.fc-alert-head { display: flex; align-items: baseline; flex-wrap: wrap; gap: var(--s-2); }
.fc-alert-gloss { margin: var(--s-1) 0 var(--s-3); }
.fc-figures {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr));
  gap: var(--s-3);
  margin-bottom: var(--s-3);
}
.fc-figure { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.fc-figure .money { text-align: left; font-size: var(--text-md); font-weight: 600; }
.fc-figure-label {
  font-size: var(--text-xs);
  text-transform: uppercase;
  letter-spacing: var(--tracking-wide);
  color: var(--fg-subtle);
  font-weight: 600;
}
.fc-figure-note { font-size: var(--text-xs); color: var(--fg-muted); }
.fc-figure-key .money { color: var(--accent); }
.fc-alert-reason { margin: 0 0 var(--s-2); }
.fc-alert-proposal { margin: 0; color: var(--fg-muted); }
.fc-proposal-tag {
  display: inline-block;
  margin-right: var(--s-2);
  font-size: var(--text-xs);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: var(--tracking-wide);
  color: var(--ok);
}
.fc-proposes { margin: var(--s-4) 0 0; max-width: 68ch; }

.fc-winner th, .fc-winner td { background: var(--accent-bg); }
.fc-winner th .badge { margin-left: var(--s-2); }
.fc-details summary { cursor: pointer; font-weight: 600; }
.fc-details summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

@media (max-width: 40rem) {
  .fc-controls .row { align-items: flex-start; }
  .fc-status { flex-basis: 100%; }
}
`;

function ensureStyles() {
  if (document.getElementById("fc-styles")) return;
  const style = document.createElement("style");
  style.id = "fc-styles";
  style.textContent = STYLE;
  document.head.appendChild(style);
}

export const meta = {
  id: "forecast",
  title: "Cash Forecast",
  // Nothing: the close report says nothing about the future, and the series this screen
  // needs is fetched on a press rather than handed over by the shell.
  needs: [],
};
