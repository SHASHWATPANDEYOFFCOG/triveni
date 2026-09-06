/* Screen 5 - Settlement decomposition.
 *
 * A merchant is paid once a day and told a single number. This screen is the
 * argument that the number is right: gross captured at the left, every named
 * deduction stepping down, and the bar the bank actually credited at the right.
 *
 * Two things make it more than a picture of arithmetic.
 *
 * **Every line carries its basis.** "MDR 1.90% of Rs 12,40,000" is checkable on the
 * back of an envelope; "fees" is not. The basis string comes from `recon/waterfall.py`
 * and is rendered verbatim - it is the difference between a number that is asserted
 * and a number that is shown its working. The GST line says it out loud: 18% *on the
 * fee, not on the sale*. Charging GST on the sale is the classic expensive mistake in
 * this domain, off by two orders of magnitude, and the basis string is the proof we
 * did not make it.
 *
 * **The axis is broken, and says so.** MDR is 1.9% of gross; GST on that fee is 0.34%.
 * On a zero-based axis every deduction is a hairline and the chart teaches nothing, so
 * the y-axis starts below the net rather than at zero. That is a lie unless it is
 * declared, so it is declared twice - a break glyph drawn across each total column and
 * a caption underneath with the floor and the real percentage. A truncated axis that
 * does not admit it is how a chart becomes a claim.
 *
 * The chart's viewBox is *measured* rather than fixed, so one SVG user unit is one CSS
 * pixel and `--text-xs` inside the chart is the same 11px as `--text-xs` everywhere
 * else. A fixed viewBox scaled to a 390px phone would shrink the labels to six pixels,
 * which is the usual reason charts get a "desktop only" caveat.
 */

import { esc, formatCompact, formatDate, formatINR, humanise, truncate } from "../format.js";
import { growBar, reduced, stagger } from "../motion.js";
import { api } from "../api.js";

/** The question that compiles to the ALL_SETTLEMENTS template in `qa/compile.py`. */
const LIST_QUESTION = "show me the settlements";

/** Starter questions for the Q&A box. Each one is known to compile - they are the
 *  documented intents, not guesses - so the box teaches instead of daring you. */
const SUGGESTIONS = [
  "show me the settlements",
  "which settlements did not balance?",
  "what are the exceptions by type?",
  "how much cash lands this week?",
];

/** Column headings for the waterfall, short enough for a narrow bar. Anything the
 *  taxonomy grows later falls through to humanise(), so a new ExceptionType renders
 *  as "Rolling reserve" rather than disappearing. */
const SHORT_LABEL = {
  fee_mdr: "MDR",
  gst_on_fee: "GST",
  tds_194o: "TDS",
  refund_offset: "Refunds",
  chargeback_hold: "Holds",
  rolling_reserve: "Reserve",
};

/* Chart geometry in CSS pixels. These are the only bare numbers in the file and they
 * are geometry, not design values - no colour, spacing rhythm or duration is decided
 * here. Everything paintable comes from a token. */
const CHART = {
  height: 300,
  heightNarrow: 236,
  narrowAt: 560,
  padTop: 26,
  padBottom: 42,
  padSide: 8,
  maxBarWidth: 76,
  valueSlot: 62, // below this slot width the in-chart value would collide; drop it
  labelSlot: 82, // ...and below this, the short label goes too, leaving the step index
  stepDelay: 90, // ms between bars, so the eye follows the money down the steps
};

export function render(container, state, helpers) {
  const close = state.close;

  container.innerHTML = `
    <div class="screen-head">
      <div class="eyebrow">Explain</div>
      <h1>Settlements</h1>
      <p>
        A settlement is short, and <strong>"fees" is not an answer</strong>. Each deduction
        is named, given the rate it was computed from, and traced back to the rows it came
        out of - then the remainder is checked against what the bank actually credited.
      </p>
      ${
        close
          ? `<p class="subtle">Across today's whole close, <span class="money">${esc(
              close.unexplained
            )}</span> survived every waterfall unexplained.</p>`
          : ""
      }
    </div>

    <div class="card">
      <div class="card-title">
        <h3>Pick a settlement</h3>
        <span class="hint">explain_settlement, over the MCP tool surface</span>
      </div>
      <div class="row" id="wf-controls">
        <button class="btn primary" id="wf-load">
          <span aria-hidden="true">▤</span> List the settlements
        </button>
        <p class="subtle" id="wf-picker-note" style="margin: 0">
          Nothing is fetched until you ask for it - this screen is handed the close, not
          the payouts.
        </p>
      </div>
    </div>

    <div id="wf-panel"></div>

    <div class="card">
      <div class="card-title">
        <h3>Ask the books a question</h3>
        <span class="hint">one of a fixed set of hand-written queries, never generated SQL</span>
      </div>
      <form class="qa-box" id="qa-form">
        <label class="sr-only" for="qa-input">Ask a question about the reconciled books</label>
        <input
          id="qa-input"
          name="question"
          type="text"
          autocomplete="off"
          placeholder="why is settlement STL-2026-03-31-001 short?"
        />
        <button class="btn primary" type="submit" id="qa-submit">Ask</button>
      </form>
      <div class="chips" id="qa-chips" style="margin-top: var(--s-3)"></div>
      <div id="qa-out" aria-live="polite"></div>
    </div>
  `;

  const ctx = {
    container,
    helpers,
    panel: container.querySelector("#wf-panel"),
    controls: container.querySelector("#wf-controls"),
    note: container.querySelector("#wf-picker-note"),
    model: null,
    observer: null,
    lastWidth: 0,
  };

  ctx.panel.innerHTML = teaser();

  container.querySelector("#wf-load").addEventListener("click", (event) => {
    loadSettlements(event.currentTarget, ctx);
  });

  wireQuestions(container);

  // The chart is redrawn on resize because its viewBox is measured; the observer is
  // the only thing this screen leaves running, so it is the only thing to clean up.
  return () => ctx.observer?.disconnect();
}

/* ------------------------------------------------------------------------- */
/* The settlement list                                                        */
/* ------------------------------------------------------------------------- */
function teaser() {
  return `
    <div class="empty">
      <h3>No settlement chosen yet</h3>
      <p>
        Choose one and this becomes a waterfall: gross captured, MDR, GST on that fee,
        TDS under section 194-O, refunds netted, holds - ending on the amount the bank
        credited and the remainder nobody could explain.
      </p>
      <p class="subtle">The same decomposition, printed rather than drawn:</p>
      <code>make demo</code>
    </div>`;
}

async function loadSettlements(button, ctx) {
  button.disabled = true;
  const original = button.innerHTML;
  button.innerHTML = '<span aria-hidden="true">◌</span> Asking…';

  // The list arrives through the Q&A path rather than a bespoke endpoint: the same
  // compiled query a merchant could ask for in words is the one this select is built
  // from, so the UI cannot show a settlement the Q&A would deny exists.
  const result = await api.mcpCall("ask", { question: LIST_QUESTION });
  button.disabled = false;
  button.innerHTML = original.replace("List", "Reload");

  const payload = result.data ?? {};
  const rows = Array.isArray(payload.rows) ? payload.rows : [];

  if (!result.ok || rows.length === 0) {
    ctx.panel.innerHTML = `
      <div class="empty">
        <h3>No settlements came back</h3>
        <p>${esc(result.error || payload.reason || "the query returned no rows")}</p>
        <p class="subtle">Build the books first, then reload:</p>
        <code>make demo</code>
      </div>`;
    return;
  }

  const shown = rows.length;
  const total = Number(payload.row_count);
  ctx.note.innerHTML = Number.isFinite(total)
    ? `<span class="subtle">${shown} of ${total} settlement(s) from <code>${esc(
        payload.intent ?? "all_settlements"
      )}</code>.</span>`
    : `<span class="subtle">${shown} settlement(s).</span>`;

  // Replace the note with a real <select>, once. A second press of the button
  // refreshes the options rather than stacking a second control beside the first.
  let picker = ctx.controls.querySelector("#wf-pick");
  if (!picker) {
    const label = document.createElement("label");
    label.className = "sr-only";
    label.setAttribute("for", "wf-pick");
    label.textContent = "Settlement to explain";
    picker = document.createElement("select");
    picker.id = "wf-pick";
    picker.className = "btn";
    picker.addEventListener("change", () => explain(picker.value, ctx));
    ctx.controls.append(label, picker);
  }

  picker.innerHTML = rows
    .map((row) => {
      const id = String(row.settlement_id ?? "");
      // The option text says which ones did not close, so the interesting settlement
      // is findable without opening each of them in turn.
      const flag = row.balanced === false || row.balanced === 0 ? " · unbalanced" : "";
      return `<option value="${esc(id)}">${esc(id)} · ${esc(
        formatDate(row.settled_on)
      )}${esc(flag)}</option>`;
    })
    .join("");

  // Default to the first, as a merchant would expect, and explain it immediately -
  // an empty panel beside a populated select is a dead end.
  picker.value = String(rows[0].settlement_id ?? "");
  explain(picker.value, ctx);
}

/* ------------------------------------------------------------------------- */
/* One settlement                                                             */
/* ------------------------------------------------------------------------- */
async function explain(settlementId, ctx) {
  if (!settlementId) return;
  ctx.panel.innerHTML = '<div class="card"><div class="skeleton" style="height: 18rem"></div></div>';

  const result = await api.mcpCall("explain_settlement", { settlement_id: settlementId });
  const payload = result.data ?? {};

  if (!result.ok || payload.ok === false || !payload.waterfall) {
    ctx.panel.innerHTML = `
      <div class="empty">
        <h3>That settlement could not be explained</h3>
        <p>${esc(result.error || payload.reason || "the tool returned no waterfall")}</p>
        <p class="subtle">Reproduce it on the command line:</p>
        <code>triveni explain ${esc(settlementId)}</code>
      </div>`;
    return;
  }

  const waterfall = normalise(payload.waterfall);
  if (!waterfall) {
    ctx.panel.innerHTML = `
      <div class="empty">
        <h3>The waterfall arrived in a shape this screen does not know</h3>
        <p>Neither <code>gross_paise</code> nor <code>gross</code> was present, so there is
        nothing honest to draw.</p>
      </div>`;
    return;
  }

  ctx.model = buildSteps(waterfall);
  paint(ctx, waterfall, payload);
  document.getElementById("live").textContent = payload.reason ?? "";
}

/**
 * Accept both shapes of the tool's waterfall.
 *
 * `Waterfall.canonical()` emits flat paise integers (`gross_paise`, `amount_paise`);
 * an earlier revision nested them as `{paise: n}`. Four lines of tolerance here means
 * a rename on the Python side degrades to a redraw rather than a blank screen, and
 * `null` - not a zero - is what comes back when neither is there, because a fabricated
 * zero rupee gross would be the worst possible failure mode on this particular screen.
 */
function normalise(raw) {
  const paise = (flat, nested) => {
    if (Number.isFinite(raw?.[flat])) return Math.trunc(raw[flat]);
    const value = raw?.[nested];
    if (Number.isFinite(value)) return Math.trunc(value);
    if (Number.isFinite(value?.paise)) return Math.trunc(value.paise);
    return null;
  };

  const gross = paise("gross_paise", "gross");
  if (gross === null) return null;

  const lines = (Array.isArray(raw.lines) ? raw.lines : [])
    .map((line) => {
      const amount = Number.isFinite(line?.amount_paise)
        ? Math.trunc(line.amount_paise)
        : Number.isFinite(line?.amount?.paise)
          ? Math.trunc(line.amount.paise)
          : null;
      if (amount === null) return null;
      return {
        component: String(line.component ?? "unknown"),
        amount,
        basis: String(line.basis ?? ""),
        tracedTo: Array.isArray(line.traced_to) ? line.traced_to.map(String) : [],
        fitted: Boolean(line.fitted),
      };
    })
    .filter(Boolean);

  return {
    settlementId: String(raw.settlement_id ?? ""),
    asOf: raw.as_of ?? "",
    gross,
    lines,
    netExpected: paise("net_expected_paise", "net_expected"),
    netReceived: paise("net_received_paise", "net_received"),
    residual: paise("residual_paise", "residual"),
    // `!== false` made an ABSENT field read as balanced, so a payload that omitted
    // or renamed it stamped a green "balanced to Rs 0" seal over books that might be
    // short by real money - and suppressed the route to triage. Defaulting to false
    // is the only safe direction here: an unbalanced settlement wrongly flagged
    // costs a glance, a short one wrongly cleared costs the merchant.
    balanced: raw.balanced === true,
  };
}

/**
 * Gross, one step per line, then the two totals.
 *
 * The intermediate running subtotals are computed here - gross plus each signed line,
 * which is exactly how `net_expected` is defined in Python - but the two total bars
 * take their heights from the payload's own `net_expected` / `net_received`. If the
 * server's arithmetic and this loop ever disagreed, the disagreement would be visible
 * as a gap rather than smoothed away by drawing my own sum twice.
 */
function buildSteps(wf) {
  const steps = [{ kind: "total", key: "gross", label: "Gross captured", value: wf.gross }];

  let running = wf.gross;
  for (const line of wf.lines) {
    const from = running;
    running += line.amount;
    steps.push({
      kind: "delta",
      line,
      label: SHORT_LABEL[line.component] ?? humanise(line.component),
      from,
      to: running,
      value: running,
    });
  }

  if (wf.netExpected !== null) {
    steps.push({ kind: "total", key: "net_expected", label: "Net expected", value: wf.netExpected });
  }
  if (wf.netReceived !== null) {
    steps.push({ kind: "total", key: "net_received", label: "Net received", value: wf.netReceived });
  }
  return steps;
}

function paint(ctx, wf, payload) {
  const deducted = wf.lines.reduce((sum, line) => sum + Math.abs(line.amount), 0);
  const share = wf.gross > 0 ? ((deducted / wf.gross) * 100).toFixed(2) : "0.00";

  ctx.panel.innerHTML = `
    <div class="card">
      <div class="card-title">
        <h3>${esc(wf.settlementId)} · settled ${esc(formatDate(wf.asOf))}</h3>
        <span class="hint">${wf.lines.length} named component(s)</span>
      </div>
      <div id="wf-chart"></div>
      <p class="subtle" id="wf-axis-note" style="margin-top: var(--s-3)"></p>
      ${gstCallout(wf)}
      <div class="row" style="margin-top: var(--s-4)">${seal(wf)}</div>
      ${residualLink(wf)}
    </div>

    <div class="card">
      <div class="card-title">
        <h3>Every line, with the basis it was computed from</h3>
        <span class="hint">${esc(share)}% of gross deducted</span>
      </div>
      <div class="scroll-x">${linesTable(wf)}</div>
      <details class="sql-proof">
        <summary>The same waterfall as the CLI prints it</summary>
        <pre>${esc(payload.rendered ?? "")}</pre>
      </details>
    </div>`;

  const host = ctx.panel.querySelector("#wf-chart");
  drawChart(host, wf, ctx.model, ctx.panel.querySelector("#wf-axis-note"), true);

  ctx.observer?.disconnect();
  ctx.lastWidth = Math.round(host.clientWidth);
  ctx.observer = new ResizeObserver(() => {
    const width = Math.round(host.clientWidth);
    // Only an actual change in width matters; ResizeObserver fires on sub-pixel
    // reflow too, and redrawing eight bars per scroll frame is heat for nothing.
    if (width === ctx.lastWidth || width === 0) return;
    ctx.lastWidth = width;
    drawChart(host, wf, ctx.model, ctx.panel.querySelector("#wf-axis-note"), false);
  });
  ctx.observer.observe(host);

  stagger([...ctx.panel.querySelectorAll("tbody tr")], { step: 45 });

  ctx.panel.querySelector("#wf-to-triage")?.addEventListener("click", () => {
    ctx.helpers?.showScreen?.("triage");
  });
}

function gstCallout(wf) {
  const gst = wf.lines.find((line) => line.component === "gst_on_fee");
  if (!gst) return "";
  // Rendered verbatim rather than paraphrased. The sentence the engine wrote is the
  // evidence; a UI restatement of it would be one more place for the two to drift.
  return `
    <p class="caveat" style="margin-top: var(--s-4)">
      <strong>GST is charged on the fee, not on the sale.</strong>
      This line is <span class="money">${esc(formatINR(Math.abs(gst.amount)))}</span>, and its
      basis reads <span class="basis">${esc(gst.basis)}</span> - two orders of magnitude
      away from what 18% of the gross would have been, which is the mistake this basis
      string exists to rule out.
    </p>`;
}

function seal(wf) {
  if (wf.balanced) {
    // The RESIDUAL, not a literal zero. Printing formatINR(0) meant the seal said
    // "balanced to ₹0.00" whether or not it was - the one figure on this screen that
    // must never be decorative, since the entire claim of Loop 2 is that the
    // waterfall closes to the paise. If a rounding-slack residual ever survives the
    // balanced check, it belongs on screen rather than papered over.
    const closed = wf.residual === null ? 0 : wf.residual;
    return `<span class="seal" data-ok="true"><span aria-hidden="true">✓</span> balanced to ${esc(
      formatINR(closed)
    )}</span>
    <span class="subtle">gross less every named component lands exactly on what the bank credited.</span>`;
  }
  const residual = wf.residual === null ? null : Math.abs(wf.residual);
  return `<span class="seal" data-ok="false"><span aria-hidden="true">!</span> unexplained
    <span class="money">${residual === null ? "—" : esc(formatINR(residual))}</span></span>
    <span class="subtle">the components could not close the gap; the remainder is money, not rounding.</span>`;
}

function residualLink(wf) {
  if (wf.balanced) return "";
  return `
    <p style="margin-top: var(--s-3)">
      <button class="btn" id="wf-to-triage">
        <span aria-hidden="true">→</span> See it as a typed exception in triage
      </button>
    </p>`;
}

function linesTable(wf) {
  const rows = [];
  let running = wf.gross;

  rows.push(`
    <tr>
      <td class="num subtle">1</td>
      <td><strong>Gross captured</strong></td>
      <td class="num money">${esc(formatINR(wf.gross))}</td>
      <td class="num money">${esc(formatINR(wf.gross))}</td>
      <td class="basis">everything this settlement swept up, before a rupee came off</td>
    </tr>`);

  wf.lines.forEach((line, index) => {
    running += line.amount;
    const traced = line.tracedTo.length
      ? `<div class="basis">← traced to ${line.tracedTo.length} id(s): ${esc(
          truncate(line.tracedTo.join(", "), 64)
        )}</div>`
      : "";
    const fitted = line.fitted
      ? `<span class="badge" data-sev="low" title="recovered from this batch by least squares, not read off the rate card">fitted</span>`
      : "";
    rows.push(`
      <tr>
        <td class="num subtle">${index + 2}</td>
        <td>${esc(humanise(line.component))} ${fitted}</td>
        <td class="num money ${line.amount < 0 ? "neg" : "pos"}">${esc(formatINR(line.amount))}</td>
        <td class="num money">${esc(formatINR(running))}</td>
        <td><div class="basis">${esc(line.basis)}</div>${traced}</td>
      </tr>`);
  });

  const totalRow = (label, value, note) =>
    value === null
      ? ""
      : `<tr>
           <td></td>
           <td><strong>${esc(label)}</strong></td>
           <td class="num money">${esc(formatINR(value))}</td>
           <td class="num money">${esc(formatINR(value))}</td>
           <td class="basis">${esc(note)}</td>
         </tr>`;

  return `
    <table>
      <caption class="sr-only">Settlement ${esc(wf.settlementId)}, gross to net</caption>
      <thead>
        <tr>
          <th class="num">#</th>
          <th>Component</th>
          <th class="num">Amount</th>
          <th class="num">Running</th>
          <th>Basis</th>
        </tr>
      </thead>
      <tbody>
        ${rows.join("")}
        ${totalRow("Net expected", wf.netExpected, "gross plus every signed line above")}
        ${totalRow("Net received", wf.netReceived, "what the bank statement actually says")}
      </tbody>
    </table>`;
}

/* ------------------------------------------------------------------------- */
/* The chart                                                                  */
/* ------------------------------------------------------------------------- */
function drawChart(host, wf, steps, noteNode, animate) {
  const width = Math.max(host.clientWidth || 0, 320);
  const height = width < CHART.narrowAt ? CHART.heightNarrow : CHART.height;
  const plotTop = CHART.padTop;
  const plotBottom = height - CHART.padBottom;
  const plotLeft = CHART.padSide;
  const plotWidth = width - CHART.padSide * 2;
  const slot = plotWidth / steps.length;
  const barWidth = Math.min(slot * 0.62, CHART.maxBarWidth);

  // The declared break. Every value in play sits between `lo` and `hi`; the floor goes
  // a fifth of that span below `lo` so the shortest total still reads as a column
  // rather than as a line, and the caption below states the floor in rupees.
  const values = steps.flatMap((step) =>
    step.kind === "total" ? [step.value] : [step.from, step.to]
  );
  const hi = Math.max(...values);
  const lo = Math.min(...values);
  const span = Math.max(hi - lo, 1);
  const floor = lo - span * 0.2;
  const ceiling = hi + span * 0.06;
  const y = (value) => plotBottom - ((value - floor) / (ceiling - floor)) * (plotBottom - plotTop);

  const showValue = slot >= CHART.valueSlot;
  const showLabel = slot >= CHART.labelSlot;

  const bars = steps.map((step, index) => {
    const x = plotLeft + slot * index + (slot - barWidth) / 2;
    const top = step.kind === "total" ? y(step.value) : y(Math.max(step.from, step.to));
    const bottom = step.kind === "total" ? plotBottom : y(Math.min(step.from, step.to));
    const barHeight = Math.max(bottom - top, 1);

    // Gold is the palette's attention colour and carries no meaning, which is exactly
    // right for a deduction: an MDR fee is not an error and painting six fee bars in
    // --bad would tell a merchant their books were on fire. Green is reserved for the
    // one bar that genuinely reports a state - the money that arrived.
    let fill = "var(--info)";
    if (step.kind === "delta") fill = step.line.amount < 0 ? "var(--accent)" : "var(--info)";
    else if (step.key === "net_received") fill = wf.balanced ? "var(--ok)" : "var(--warn)";

    // A negative step hangs down from the previous subtotal, so it should grow
    // downward from its top edge; everything else rises from its base.
    const origin = step.kind === "delta" && step.line.amount < 0 ? "center top" : "center bottom";

    const caption = showValue
      ? `<text class="wf-value" x="${(x + barWidth / 2).toFixed(1)}" y="${(top - 7).toFixed(
          1
        )}" text-anchor="middle">${esc(formatCompact(step.value))}</text>`
      : "";

    const name = showLabel
      ? `<text class="wf-label" x="${(x + barWidth / 2).toFixed(1)}" y="${(
          plotBottom + 30
        ).toFixed(1)}" text-anchor="middle">${esc(truncate(step.label, 12))}</text>`
      : "";

    return `
      <g>
        ${caption}
        <rect class="wf-bar" data-step="${index}" x="${x.toFixed(1)}" y="${top.toFixed(1)}"
              width="${barWidth.toFixed(1)}" height="${barHeight.toFixed(1)}"
              rx="2" fill="${fill}"
              style="transform-box: fill-box; transform-origin: ${origin}" />
        ${step.kind === "total" ? breakGlyph(x, barWidth, plotBottom - 12) : ""}
        <text class="wf-label" x="${(x + barWidth / 2).toFixed(1)}" y="${(plotBottom + 15).toFixed(
          1
        )}" text-anchor="middle">${index + 1}</text>
        ${name}
      </g>`;
  });

  // Connectors sit at each running subtotal, so the eye can follow the level across
  // the gap instead of guessing whether the next bar starts where the last one ended.
  const connectors = steps
    .slice(0, -1)
    .map((step, index) => {
      const level = step.kind === "total" ? step.value : step.to;
      const from = plotLeft + slot * index + (slot + barWidth) / 2;
      const to = plotLeft + slot * (index + 1) + (slot - barWidth) / 2;
      return `<line x1="${from.toFixed(1)}" y1="${y(level).toFixed(1)}" x2="${to.toFixed(
        1
      )}" y2="${y(level).toFixed(1)}" stroke="var(--rule-strong)" stroke-width="1"
        stroke-dasharray="3 3" />`;
    })
    .join("");

  host.innerHTML = `
    <svg class="waterfall-svg" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}"
         role="img" aria-labelledby="wf-title wf-desc">
      <title id="wf-title">Settlement ${esc(wf.settlementId)}, gross to net</title>
      <desc id="wf-desc">${esc(altText(wf))}</desc>
      <line x1="${plotLeft}" y1="${plotBottom}" x2="${(plotLeft + plotWidth).toFixed(1)}"
            y2="${plotBottom}" stroke="var(--rule-strong)" stroke-width="1" />
      ${connectors}
      ${bars.join("")}
    </svg>`;

  noteNode.innerHTML =
    `The y-axis starts at <span class="money">${esc(formatINR(Math.round(floor)))}</span>, ` +
    `not at zero - the deductions are a few percent of gross and would be hairlines on a ` +
    `zero-based axis. The break is drawn across each total column. Bar labels are rounded; ` +
    `the exact paise are in the table below.`;

  if (!animate || reduced()) return;
  // growBar is already a no-op under reduced motion; the guard above just avoids
  // re-animating on every resize, which would be a chart that flinches when you turn
  // your phone.
  host.querySelectorAll(".wf-bar").forEach((bar, index) => {
    growBar(bar, "scaleY", 520, index * CHART.stepDelay);
  });
}

/** The zig-zag that admits the axis does not start at zero. */
function breakGlyph(x, width, atY) {
  const points = [];
  for (let offset = 0; offset <= width; offset += 6) {
    const px = Math.min(x + offset, x + width);
    points.push(`${px.toFixed(1)},${(atY + (points.length % 2 ? 3 : -3)).toFixed(1)}`);
  }
  const path = points.join(" ");
  // Painted twice: once thick in the card's own background to cut the bar, once thin
  // in the rule colour so the cut reads as deliberate rather than as a rendering fault.
  return `
    <polyline points="${path}" fill="none" stroke="var(--bg-raised)" stroke-width="6" />
    <polyline points="${path}" fill="none" stroke="var(--rule-strong)" stroke-width="1" />`;
}

function altText(wf) {
  const parts = [
    `Gross ${formatINR(wf.gross)}.`,
    ...wf.lines.map((line) => `${humanise(line.component)} ${formatINR(line.amount)}, ${line.basis}.`),
  ];
  if (wf.netReceived !== null) parts.push(`Net received ${formatINR(wf.netReceived)}.`);
  parts.push(
    wf.balanced
      ? `Balanced to ${formatINR(0)}.`
      : `${formatINR(wf.residual ?? 0)} unexplained, raised as a typed exception.`
  );
  return parts.join(" ");
}

/* ------------------------------------------------------------------------- */
/* Grounded Q&A                                                               */
/* ------------------------------------------------------------------------- */
function wireQuestions(container) {
  const form = container.querySelector("#qa-form");
  const input = container.querySelector("#qa-input");
  const submit = container.querySelector("#qa-submit");
  const out = container.querySelector("#qa-out");

  container.querySelector("#qa-chips").innerHTML = SUGGESTIONS.map(
    (question) => `<button class="chip" type="button" data-q="${esc(question)}">${esc(question)}</button>`
  ).join("");

  container.querySelectorAll("#qa-chips .chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      input.value = chip.dataset.q;
      form.requestSubmit();
    });
  });

  out.innerHTML = `
    <p class="subtle" style="margin-top: var(--s-3)">
      Every numeral in an answer is checked against the rows the query returned. When one
      cannot be traced, Triveni abstains and shows you the table instead - which is a
      better outcome than a confident wrong number, and the only one that survives audit.
    </p>`;

  // A real <form>, so Enter submits without a keydown handler and the button is a
  // genuine submit control rather than a div listening for clicks.
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const question = input.value.trim();
    if (!question) return;

    submit.disabled = true;
    submit.textContent = "Asking…";
    out.innerHTML = '<div class="skeleton" style="height: 5rem; margin-top: var(--s-3)"></div>';

    const result = await api.ask(question);
    submit.disabled = false;
    submit.textContent = "Ask";
    renderAnswer(out, result);
  });
}

function renderAnswer(out, result) {
  if (!result.ok) {
    out.innerHTML = `
      <div class="empty" style="margin-top: var(--s-3)">
        <h3>The question never reached the books</h3>
        <p>${esc(result.error)}</p>
        <code>make run</code>
      </div>`;
    return;
  }

  const answer = result.data ?? {};
  const abstained = answer.answered === false;
  const rows = Array.isArray(answer.rows) ? answer.rows : [];
  const grounding = answer.grounding ?? null;

  const groundingLine = grounding
    ? `<p class="why">${grounding.checked?.length ?? 0} figure(s) checked against ${
        grounding.sources ?? 0
      } value(s) in the result${
        grounding.ungrounded?.length
          ? `; <strong>${grounding.ungrounded.length} could not be traced</strong>: ${esc(
              truncate(grounding.ungrounded.join(", "), 80)
            )}`
          : "; none untraceable"
      }.</p>`
    : `<p class="why">No grounding check ran - the question never compiled to a query.</p>`;

  const table = rows.length
    ? `<div class="scroll-x" style="margin-top: var(--s-3)">${rowsTable(rows)}</div>`
    : "";

  // Abstention gets the table inline and unhidden; a confident answer gets it folded
  // away behind a summary, because there the table is a receipt rather than the point.
  const evidence = abstained
    ? `<p class="subtle" style="margin-top: var(--s-3)">Here is the table I would have used:</p>${table}`
    : rows.length
      ? `<details class="sql-proof"><summary>The ${rows.length} row(s) the answer was checked against</summary>${table}</details>`
      : "";

  out.innerHTML = `
    <div class="qa-answer" data-abstained="${abstained}">
      <p style="margin-bottom: var(--s-2)">
        <span class="badge" data-state="${abstained ? "abstained" : "matched"}">${
          abstained ? "abstained" : "answered"
        }</span>
      </p>
      <p><strong>${esc(answer.text ?? "")}</strong></p>
      <p class="why">${esc(answer.reason ?? "")}</p>
      ${groundingLine}
      <p class="why">
        Matched the hand-written query <code>${esc(answer.intent ?? "—")}</code>${
          answer.matched_by ? ` on ${esc(answer.matched_by)}` : ""
        }. Nothing here was generated SQL.
      </p>
      ${
        answer.sql
          ? `<details class="sql-proof">
               <summary>The SQL that was executed - this is the proof</summary>
               <pre>${esc(answer.sql)}</pre>
             </details>`
          : ""
      }
      ${evidence}
    </div>`;
}

function rowsTable(rows) {
  const columns = [...new Set(rows.flatMap((row) => Object.keys(row)))];
  const head = columns
    .map((column) => `<th class="${isMoney(column) ? "num" : ""}">${esc(humanise(column))}</th>`)
    .join("");
  const body = rows
    .map(
      (row) =>
        `<tr>${columns.map((column) => cell(column, row[column])).join("")}</tr>`
    )
    .join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

/** The warehouse names every rupee column `*_paise`, and that suffix is the contract. */
function isMoney(column) {
  return String(column).endsWith("_paise");
}

function cell(column, value) {
  if (value === null || value === undefined) return `<td class="subtle">—</td>`;
  if (isMoney(column) && Number.isFinite(Number(value))) {
    // Integer paise, formatted here rather than in SQL - the same formatter the rest of
    // the product uses, so a figure never reads differently on two screens.
    return `<td class="num money">${esc(formatINR(Math.trunc(Number(value))))}</td>`;
  }
  if (typeof value === "boolean") return `<td>${value ? "yes" : "no"}</td>`;
  if (/^\d{4}-\d{2}-\d{2}/.test(String(value))) return `<td>${esc(formatDate(value))}</td>`;
  return `<td>${esc(truncate(String(value), 48))}</td>`;
}

export const meta = {
  id: "waterfall",
  title: "Settlement decomposition",
  needs: [],
};
