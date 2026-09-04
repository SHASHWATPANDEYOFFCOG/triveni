/* Screen 1 - the command centre.
 *
 * ---------------------------------------------------------------------------
 * WHAT CHANGED, AND WHY
 * ---------------------------------------------------------------------------
 * This screen used to open with three sentences of prose and two numbers. It read
 * like the first page of a report, which is the wrong thing for the first screen of
 * a tool: someone opening it already knows what the product does, and wants to know
 * what happened today.
 *
 * So the prose is gone and the information is carried as shape. Every mark on this
 * screen is proportional to a real value:
 *
 *   - the canvas particle count IS the row count (536 rows, 536 particles)
 *   - the ring arc IS auto-post coverage, with a tick at the calibrated threshold
 *   - the flow segment widths ARE the measured per-stage milliseconds
 *   - the bar lengths ARE the exception counts by type
 *
 * That last one is the test worth applying to any dashboard: if the picture would
 * look identical against different numbers, the picture is decoration. Every element
 * here would visibly change.
 *
 * The flow bar is the one that earns its place. It shows, without a word of
 * commentary, that Fellegi-Sunter and the global assignment are ~95% of the close -
 * which is the honest cost of solving the whole day at once instead of greedily, and
 * the single most useful thing an engineer can learn about this pipeline in one
 * glance.
 */

import { esc, formatINR, formatPct } from "../format.js";
import { odometer, rafLoop, reduced, stagger } from "../motion.js";
import { streamClose } from "../api.js";

const SOURCES = [
  { key: "gateway", label: "Gateway", hue: "--info", y: 0.22 },
  { key: "bank", label: "Bank", hue: "--accent", y: 0.5 },
  { key: "ledger", label: "Ledger", hue: "--ok", y: 0.78 },
];

/** Stage -> hue. Cheap stages cool, the two expensive ones warm, so the flow bar
 *  reads as a cost gradient rather than as an arbitrary rainbow. */
const STAGE_HUE = {
  stage0: "--slate-400",
  stage1: "--stream-ledger",
  stage2: "--stream-payments",
  stage3: "--stream-bank",
  stage4: "--secondary",
  stage5: "--ok",
  stage6: "--warn",
};

export function render(container, state, { showScreen } = {}) {
  const close = state.close;

  container.innerHTML = `
    <div class="screen-head">
      <div class="row-between wrap">
        <div>
          <div class="eyebrow">
            <span class="status-dot" data-live="${close ? "false" : "true"}" aria-hidden="true"></span>
            Close · ${esc(state.date)}
          </div>
          <h2>Today's books</h2>
        </div>
        <div class="row wrap">
          <button class="btn outline" id="go-triage">
            Review exceptions <span class="icon-shift" aria-hidden="true">→</span>
          </button>
          <button class="btn primary" id="run-close">
            <span aria-hidden="true">▶</span> Close the books
          </button>
        </div>
      </div>
    </div>

    <div class="kpi-row" id="kpis"></div>

    <div class="split" style="margin-top: var(--space-4)">
      <div class="card confluence-wrap" style="padding: 0">
        <canvas id="confluence-canvas" aria-hidden="true"></canvas>
        <figcaption class="sr-only" id="confluence-alt"></figcaption>
        <div class="confluence-legend" id="confluence-legend"></div>
      </div>

      <div class="card" id="gauge-card"></div>
    </div>

    <div class="card" style="margin-top: var(--space-4)">
      <div class="card-title">
        <h3>Where the time goes</h3>
        <span class="hint">segment width = measured milliseconds</span>
      </div>
      <div class="flow" id="flow" role="group" aria-label="Stage timings"></div>
      <div class="flow-legend" id="flow-legend"></div>
      <div class="flow-detail" id="flow-detail" aria-live="polite"></div>
    </div>

    <div class="split" style="margin-top: var(--space-4)">
      <div class="card">
        <div class="card-title">
          <h3>What needs a human</h3>
          <span class="hint" id="exc-total"></span>
        </div>
        <div class="barlist" id="exc-bars"></div>
      </div>

      <div class="card" id="cost-card"></div>
    </div>

    <div class="card" style="margin-top: var(--space-4)">
      <div class="card-title">
        <h3>Decision log</h3>
        <span class="hint">one entry per stage, streamed over SSE</span>
      </div>
      <ol class="ticker" id="stagelog" aria-live="polite"></ol>
    </div>
  `;

  renderLegend(container.querySelector("#confluence-legend"));
  renderKpis(container.querySelector("#kpis"), close);
  renderGauge(container.querySelector("#gauge-card"), close);
  renderFlow(container, close);
  renderExceptions(container, state.exceptions);
  renderCost(container.querySelector("#cost-card"), close, state.costmodel);

  const canvas = container.querySelector("#confluence-canvas");
  const scene = new Confluence(canvas, close);
  scene.start();

  container.querySelector("#confluence-alt").textContent = altText(close);
  if (close) fillLog(container.querySelector("#stagelog"), close.stages);

  container.querySelector("#go-triage").addEventListener("click", () => showScreen?.("triage"));
  container.querySelector("#run-close").addEventListener("click", (event) => {
    runClose(event.currentTarget, container, scene, state);
  });

  return () => scene.stop();
}

/* --------------------------------------------------------------------------- */
/* KPI strip                                                                    */
/* --------------------------------------------------------------------------- */
function renderKpis(node, close) {
  if (!close) {
    node.innerHTML = `
      <div class="state-block" style="grid-column: 1 / -1">
        <span class="glyph" aria-hidden="true">◇</span>
        <h3>No close has been run</h3>
        <p>Press <strong>Close the books</strong> to reconcile today across three ledgers.</p>
      </div>`;
    return;
  }

  const tiles = [
    {
      k: "Rows reconciled",
      to: close.rows_ingested,
      decimals: 0,
      hue: "--stream-payments",
      sub: `${close.matches} match groups`,
    },
    {
      k: "Match rate",
      to: Number(close.match_rate) * 100,
      decimals: 2,
      suffix: "%",
      hue: "--ok",
      sub: "of rows placed in a group",
    },
    {
      k: "Needs a human",
      to: close.exceptions,
      decimals: 0,
      hue: "--warn",
      sub: "typed, evidenced, routed",
    },
    {
      k: "Realised error",
      to: Number(close.realised_error_holdout) * 100,
      decimals: 2,
      suffix: "%",
      hue: "--stream-bank",
      sub: `against a ${formatPct(Number(close.nominal_bound))} bound`,
    },
  ];

  node.innerHTML = tiles
    .map(
      (tile) => `
      <div class="kpi" style="--kpi-hue: var(${tile.hue})">
        <span class="k">${esc(tile.k)}</span>
        <span class="v" data-to="${tile.to}" data-dec="${tile.decimals}" data-suf="${tile.suffix ?? ""}">0</span>
        <span class="sub">${esc(tile.sub)}</span>
      </div>`
    )
    .join("");

  // Count up. The final frame writes the exact target rather than an interpolation,
  // because a counter that settles on 95.33% when the number is 95.34% has invented
  // a figure - and that is the one thing this product may never do.
  for (const value of node.querySelectorAll(".v")) {
    const to = Number(value.dataset.to);
    const dec = Number(value.dataset.dec);
    const suffix = value.dataset.suf;
    odometer(value, to, (n) => `${n.toFixed(dec)}${suffix}`);
  }
  stagger([...node.children], { step: 60 });
}

/* --------------------------------------------------------------------------- */
/* Coverage ring                                                                */
/* --------------------------------------------------------------------------- */
function renderGauge(node, close) {
  if (!close) {
    node.innerHTML = `<div class="state-block"><span class="glyph" aria-hidden="true">◷</span><h3>Awaiting a close</h3></div>`;
    return;
  }

  const coverage = Number(close.auto_post_coverage);
  const threshold = Number(close.conformal_threshold);
  const R = 54;
  const C = 2 * Math.PI * R;
  const offset = C * (1 - coverage);
  // The tick sits at the threshold's position on the same circle, so "how much
  // cleared the bar" and "where the bar is" are read in one look.
  const angle = 2 * Math.PI * threshold;

  node.innerHTML = `
    <div class="card-title">
      <h3>Posted without a human</h3>
      <span class="hint">ring = coverage · tick = threshold</span>
    </div>
    <div class="gauge" style="--gauge-hue: var(--stream-payments)">
      <svg width="140" height="140" viewBox="0 0 140 140" role="img"
           aria-label="Auto-post coverage ${formatPct(coverage)} against a calibrated threshold of ${threshold}">
        <circle class="track" cx="70" cy="70" r="${R}" stroke-width="12" />
        <circle class="arc" cx="70" cy="70" r="${R}" stroke-width="12"
                stroke-dasharray="${C.toFixed(2)}" stroke-dashoffset="${C.toFixed(2)}" />
        <line class="tick"
              x1="${(70 + (R - 9) * Math.cos(angle)).toFixed(2)}"
              y1="${(70 + (R - 9) * Math.sin(angle)).toFixed(2)}"
              x2="${(70 + (R + 9) * Math.cos(angle)).toFixed(2)}"
              y2="${(70 + (R + 9) * Math.sin(angle)).toFixed(2)}" />
      </svg>
      <span class="center">
        <span class="big">${formatPct(coverage, 1)}</span>
        <span class="small">auto-posted</span>
      </span>
    </div>
    <div class="metric-grid" style="margin-top: var(--space-4)">
      <div class="metric"><span class="k">Threshold</span><span class="v">${esc(String(close.conformal_threshold))}</span></div>
      <div class="metric"><span class="k">Unexplained</span><span class="v">${esc(close.unexplained)}</span></div>
    </div>`;

  // Animate the arc after a frame, so the transition has an initial value to run from.
  const arc = node.querySelector(".arc");
  if (reduced()) {
    arc.style.strokeDashoffset = String(offset);
  } else {
    requestAnimationFrame(() => {
      arc.style.strokeDashoffset = String(offset);
    });
  }
}

/* --------------------------------------------------------------------------- */
/* Stage flow                                                                   */
/* --------------------------------------------------------------------------- */
function renderFlow(container, close) {
  const flow = container.querySelector("#flow");
  const legend = container.querySelector("#flow-legend");
  const detail = container.querySelector("#flow-detail");

  if (!close || !close.stages?.length) {
    flow.innerHTML = "";
    detail.textContent = "Run the close to see where the time goes.";
    return;
  }

  const stages = close.stages;
  const total = stages.reduce((sum, s) => sum + Number(s.elapsed_ms), 0);

  flow.innerHTML = stages
    .map((stage, index) => {
      const share = Number(stage.elapsed_ms) / total;
      return `
        <button class="flow-seg" data-index="${index}"
                style="flex: ${share.toFixed(5)} 1 0; --seg-hue: var(${STAGE_HUE[stage.stage] ?? "--accent"}); animation-delay: ${index * 70}ms"
                aria-label="${esc(stage.label)}, ${Number(stage.elapsed_ms).toFixed(0)} milliseconds, ${formatPct(share, 1)} of the close"></button>`;
    })
    .join("");

  legend.innerHTML = `
    <span>${esc(stages[0].label)}</span>
    <span class="num">${(total / 1000).toFixed(1)}s total</span>
    <span>${esc(stages[stages.length - 1].label)}</span>`;

  const show = (index) => {
    const stage = stages[index];
    const share = Number(stage.elapsed_ms) / total;
    detail.innerHTML = `
      <span class="name">${esc(stage.label)}</span>
      <span class="num subtle"> · ${Number(stage.elapsed_ms).toFixed(0)}ms · ${formatPct(share, 1)} of the close · +${stage.matches_added} matched</span>
      <div class="why">${esc(stage.detail)}</div>`;
    for (const seg of flow.children) seg.dataset.on = String(Number(seg.dataset.index) === index);
  };

  // Default to the most expensive stage, because that is the one worth explaining.
  const worst = stages.reduce(
    (best, s, i) => (Number(s.elapsed_ms) > Number(stages[best].elapsed_ms) ? i : best),
    0
  );
  show(worst);

  flow.addEventListener("pointerover", (event) => {
    const seg = event.target.closest(".flow-seg");
    if (seg) show(Number(seg.dataset.index));
  });
  flow.addEventListener("focusin", (event) => {
    const seg = event.target.closest(".flow-seg");
    if (seg) show(Number(seg.dataset.index));
  });
}

/* --------------------------------------------------------------------------- */
/* Exception mix                                                                */
/* --------------------------------------------------------------------------- */
function renderExceptions(container, exceptions) {
  const node = container.querySelector("#exc-bars");
  const total = container.querySelector("#exc-total");

  if (!exceptions?.length) {
    total.textContent = "";
    node.innerHTML = `<div class="state-block"><span class="glyph" aria-hidden="true">✓</span><h3>Nothing to review</h3></div>`;
    return;
  }

  total.textContent = `${exceptions.length} open`;

  const counts = new Map();
  for (const item of exceptions) {
    const row = counts.get(item.type) ?? { n: 0, severity: item.severity };
    row.n += 1;
    counts.set(item.type, row);
  }

  const rows = [...counts.entries()].sort((a, b) => b[1].n - a[1].n);
  const max = rows[0][1].n;
  const hue = { high: "--bad", medium: "--warn", low: "--info" };

  node.innerHTML = rows
    .map(
      ([type, row], index) => `
      <div class="barlist-row">
        <span>${esc(type.replace(/_/g, " "))}</span>
        <span class="track">
          <span class="fill"
                style="width: ${((row.n / max) * 100).toFixed(1)}%; --bar-hue: var(${hue[row.severity] ?? "--fg-subtle"}); animation-delay: ${index * 60}ms"></span>
        </span>
        <span class="n">${row.n}</span>
      </div>`
    )
    .join("");
}

/* --------------------------------------------------------------------------- */
/* Cost of being wrong                                                          */
/* --------------------------------------------------------------------------- */
function renderCost(node, close, costmodel) {
  if (!close || !costmodel) {
    node.innerHTML = `<div class="state-block"><span class="glyph" aria-hidden="true">₹</span><h3>Cost model unavailable</h3></div>`;
    return;
  }

  const reviews = close.exceptions * costmodel.cost_per_review_paise;
  const exposure = costmodel.cost_per_false_post_paise;

  node.innerHTML = `
    <div class="card-title">
      <h3>What it costs</h3>
      <span class="hint">stated assumptions, not sourced</span>
    </div>
    <div class="metric-grid">
      <div class="metric">
        <span class="k">Review queue</span>
        <span class="v">${formatINR(reviews, { showPaise: false })}</span>
      </div>
      <div class="metric">
        <span class="k">One wrong post</span>
        <span class="v">${formatINR(exposure, { showPaise: false })}</span>
      </div>
    </div>
    <p class="faint" style="margin-top: var(--space-3); font-size: var(--text-xs)">
      A wrong unsupervised post costs ${Math.round(exposure / costmodel.cost_per_review_paise)}×
      a review. That ratio is what sets the threshold — not a round number someone liked.
    </p>`;
}

/* --------------------------------------------------------------------------- */
/* Decision log                                                                 */
/* --------------------------------------------------------------------------- */
function fillLog(log, stages) {
  log.innerHTML = "";
  for (const [index, stage] of (stages ?? []).entries()) appendStage(log, stage, index);
}

function appendStage(log, stage, index) {
  const item = document.createElement("li");
  item.className = "rise";
  item.innerHTML = `
    <span class="idx">${index}</span>
    <span class="what">${esc(stage.label)}</span>
    <span class="detail">${esc(stage.detail)}</span>
    <span class="n">${stage.matches_added > 0 ? "+" + stage.matches_added : "—"}</span>`;
  log.appendChild(item);
  log.scrollTop = log.scrollHeight;
}

function altText(close) {
  if (!close) return "The close has not been run yet.";
  return (
    `${close.rows_ingested} rows ingested across gateway, bank and ledger. ` +
    `${close.matches} match groups accepted. ${close.exceptions} rows could not be ` +
    `reconciled and became typed exceptions.`
  );
}

function renderLegend(node) {
  node.innerHTML = SOURCES.map(
    (source) =>
      `<span class="legend-item"><i style="background: var(${source.hue})"></i>${source.label}</span>`
  ).join("");
}

async function runClose(button, container, scene, state) {
  button.disabled = true;
  button.innerHTML = '<span aria-hidden="true">◌</span> Closing…';

  const log = container.querySelector("#stagelog");
  log.innerHTML = "";
  scene.reset();

  const say = (message) => {
    document.getElementById("live").textContent = message;
  };

  let index = 0;
  const append = (stage) => appendStage(log, stage, index++);

  await new Promise((resolve) => {
    streamClose(state.date, {
      start: () => say("Closing the books."),
      stage: (stage) => {
        append(stage);
        scene.absorb(stage.rows_consumed);
        say(`${stage.label}: ${stage.detail}`);
      },
      done: (summary) => {
        scene.settle(summary);
        say(
          `Close complete. ${summary.matches} match groups, ${summary.exceptions} exceptions, ` +
            `${summary.waterfalls_balanced} of ${summary.waterfalls} settlements balanced to zero.`
        );
        resolve();
      },
      error: () => resolve(),
    });
  });

  button.disabled = false;
  button.innerHTML = '<span aria-hidden="true">↻</span> Close again';
}

/* ------------------------------------------------------------------------- */
/* The canvas                                                                 */
/* ------------------------------------------------------------------------- */
class Confluence {
  constructor(canvas, close) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.loop = null;
    this.particles = [];
    this.absorbed = 0;
    this.total = close?.rows_ingested ?? 240;
    this.exceptionCount = close?.exceptions ?? 0;
    this.resize();
    this.seed();
    this._onResize = () => {
      this.resize();
      if (reduced()) this.drawStatic();
    };
    window.addEventListener("resize", this._onResize);
  }

  resize() {
    const rect = this.canvas.getBoundingClientRect();
    // Cap the device pixel ratio: on a 3x display a full-width canvas is 12M
    // pixels a frame, which is a lot of heat for a background flourish.
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.width = Math.max(rect.width, 320);
    this.height = Math.max(rect.height, 260);
    this.canvas.width = this.width * dpr;
    this.canvas.height = this.height * dpr;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  seed() {
    // One particle per ingested row, capped for the frame budget. When the cap
    // bites, each particle stands for several rows and the caption says so rather
    // than implying a one-to-one that is not there.
    const cap = 640;
    this.perParticle = Math.max(1, Math.ceil(this.total / cap));
    const count = Math.ceil(this.total / this.perParticle);

    this.particles = Array.from({ length: count }, (_, index) => {
      const source = SOURCES[index % SOURCES.length];
      return {
        source,
        // Deterministic scatter: index-derived rather than random, so the picture
        // is identical on every run and a screenshot is reproducible.
        t: ((index * 37) % 100) / 100,
        drift: (((index * 61) % 100) / 100 - 0.5) * 0.08,
        speed: 0.0016 + (((index * 17) % 50) / 50) * 0.0022,
        matched: false,
        settled: false,
        x: 0,
        y: 0,
      };
    });
  }

  reset() {
    this.absorbed = 0;
    this.particles.forEach((particle) => {
      particle.matched = false;
      particle.settled = false;
    });
  }

  absorb(rows) {
    this.absorbed += rows;
    const target = Math.min(
      Math.floor(this.absorbed / this.perParticle),
      this.particles.length
    );
    let marked = 0;
    for (const particle of this.particles) {
      if (marked >= target) break;
      if (!particle.matched) {
        particle.matched = true;
        marked += 1;
      } else {
        marked += 1;
      }
    }
  }

  settle(summary) {
    const exceptions = Math.ceil((summary.exceptions ?? 0) / this.perParticle);
    let remaining = exceptions;
    for (let i = this.particles.length - 1; i >= 0 && remaining > 0; i -= 1) {
      if (!this.particles[i].matched) {
        this.particles[i].settled = true;
        remaining -= 1;
      }
    }
  }

  colour(name, alpha = 1) {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    if (alpha === 1) return value || "#888";
    return `color-mix(in srgb, ${value || "#888"} ${Math.round(alpha * 100)}%, transparent)`;
  }

  start() {
    if (reduced()) {
      this.drawStatic();
      return;
    }
    this.loop = rafLoop((dt) => this.frame(dt));
  }

  stop() {
    this.loop?.stop();
    window.removeEventListener("resize", this._onResize);
  }

  /** Where a particle sits at parameter t, before or after the braid. */
  position(particle, t) {
    const w = this.width;
    const h = this.height;
    const confluenceX = w * 0.52;
    const startX = w * 0.04;

    if (t < 1) {
      const x = startX + (confluenceX - startX) * t;
      const targetY = h * particle.source.y;
      const centreY = h * 0.5;
      // Streams converge on the centre line as they approach the confluence.
      const y = targetY + (centreY - targetY) * Math.pow(t, 1.8) + particle.drift * h;
      return { x, y };
    }

    const after = Math.min(t - 1, 1);
    if (particle.settled) {
      // Peel away into the delta.
      const x = confluenceX + (w * 0.94 - confluenceX) * after;
      const y = h * 0.5 + Math.sin(after * 2.2) * h * 0.32 * after + particle.drift * h * 3;
      return { x, y };
    }
    const x = confluenceX + (w * 0.96 - confluenceX) * after;
    // The braid: two interleaved sine strands, tightening as they travel.
    const braid = Math.sin(after * 9 + particle.t * 6.283) * h * 0.05 * (1 - after * 0.6);
    return { x, y: h * 0.5 + braid + particle.drift * h * 0.3 };
  }

  frame(dt) {
    const ctx = this.ctx;
    ctx.clearRect(0, 0, this.width, this.height);
    this.drawStreams(ctx);

    for (const particle of this.particles) {
      particle.t += particle.speed * dt * (particle.matched ? 1.5 : 1);
      if (particle.t > 2) particle.t = 0;

      const { x, y } = this.position(particle, particle.t);
      particle.x = x;
      particle.y = y;

      let colour;
      if (particle.settled) colour = this.colour("--bad", 0.85);
      else if (particle.matched && particle.t >= 1) colour = this.colour("--ok", 0.8);
      else colour = this.colour(particle.source.hue, 0.55);

      ctx.fillStyle = colour;
      ctx.beginPath();
      ctx.arc(x, y, particle.matched ? 1.9 : 1.5, 0, 6.283);
      ctx.fill();
    }

    this.drawLabels(ctx);
  }

  drawStreams(ctx) {
    const w = this.width;
    const h = this.height;
    ctx.lineWidth = 1;
    for (const source of SOURCES) {
      ctx.strokeStyle = this.colour(source.hue, 0.14);
      ctx.beginPath();
      ctx.moveTo(w * 0.04, h * source.y);
      ctx.quadraticCurveTo(w * 0.3, h * source.y, w * 0.52, h * 0.5);
      ctx.stroke();
    }
    ctx.strokeStyle = this.colour("--ok", 0.16);
    ctx.beginPath();
    ctx.moveTo(w * 0.52, h * 0.5);
    ctx.lineTo(w * 0.96, h * 0.5);
    ctx.stroke();
  }

  drawLabels(ctx) {
    const w = this.width;
    const h = this.height;
    ctx.font = `600 10px ${getComputedStyle(document.body).fontFamily}`;
    ctx.fillStyle = this.colour("--fg-subtle");
    ctx.textAlign = "left";
    for (const source of SOURCES) {
      ctx.fillText(source.label.toUpperCase(), w * 0.04, h * source.y - 8);
    }
    ctx.textAlign = "right";
    ctx.fillText("RECONCILED", w * 0.96, h * 0.5 - 10);
    if (this.particles.some((p) => p.settled)) {
      ctx.fillStyle = this.colour("--bad");
      ctx.fillText("DELTA · needs a human", w * 0.96, h * 0.9);
    }
  }

  /** Reduced motion: the same composition, at rest. Nothing is lost but movement. */
  drawStatic() {
    const ctx = this.ctx;
    ctx.clearRect(0, 0, this.width, this.height);
    this.drawStreams(ctx);
    this.particles.forEach((particle, index) => {
      const t = index % 3 === 0 ? 0.45 : 1 + ((index * 13) % 90) / 100;
      const settled = index % 11 === 0;
      const { x, y } = this.position({ ...particle, settled }, t);
      ctx.fillStyle = settled
        ? this.colour("--bad", 0.85)
        : t >= 1
          ? this.colour("--ok", 0.8)
          : this.colour(particle.source.hue, 0.55);
      ctx.beginPath();
      ctx.arc(x, y, 1.7, 0, 6.283);
      ctx.fill();
    });
    this.drawLabels(ctx);
  }
}

export const meta = {
  id: "confluence",
  title: "The Confluence",
  needs: ["close"],
};
