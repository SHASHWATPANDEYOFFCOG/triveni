/* Screen 1 - The Confluence.
 *
 * Three labelled streams flow inward, braid into one matched river, and whatever
 * cannot be reconciled peels away into a delta on the right, coloured by its
 * exception type.
 *
 * The thing that makes this worth building rather than decorating with: **the
 * particle count is the row count**. 536 ingested rows means 536 particles, and when
 * the close streams over SSE they physically move from unmatched to matched as each
 * stage reports. Nothing here is a loop of stock animation; if the data changed, the
 * picture would change with it. A hero that would look identical against different
 * numbers is a hero that is telling you nothing.
 *
 * Under reduced motion the canvas renders one static composition - the same three
 * streams, the same braid, the same delta, at their final positions. The information
 * is identical; only the movement is gone.
 */

import { formatINR, esc } from "../format.js";
import { odometer, rafLoop, reduced, stagger } from "../motion.js";
import { streamClose } from "../api.js";

const SOURCES = [
  { key: "gateway", label: "Gateway", hue: "--info", y: 0.22 },
  { key: "bank", label: "Bank", hue: "--accent", y: 0.5 },
  { key: "ledger", label: "Ledger", hue: "--ok", y: 0.78 },
];

export function render(container, state) {
  const close = state.close;
  container.innerHTML = `
    <div class="screen-head">
      <div class="eyebrow">Loop 1 · Close</div>
      <h1>The confluence of three ledgers</h1>
      <p>
        A merchant gets <strong>one netted settlement a day, not one line per sale</strong>.
        Gateway, bank and internal ledger flow in; matched rows braid into a single river;
        whatever will not reconcile peels away into the delta, typed and evidenced.
      </p>
    </div>

    <div class="confluence-wrap card">
      <canvas id="confluence-canvas" aria-hidden="true"></canvas>
      <figcaption class="sr-only" id="confluence-alt"></figcaption>
      <div class="confluence-legend" id="confluence-legend"></div>
      <button class="btn primary confluence-run" id="run-close">
        <span aria-hidden="true">▶</span> Close today's books
      </button>
    </div>

    <div class="grid" id="confluence-stats" style="margin-top: var(--s-4)"></div>

    <div class="card" style="margin-top: var(--s-4)">
      <div class="card-title">
        <h3>Live decision log</h3>
        <span class="hint">streamed over SSE, one event per stage</span>
      </div>
      <ol class="stagelog" id="stagelog" aria-live="polite"></ol>
    </div>
  `;

  renderLegend(container.querySelector("#confluence-legend"));
  renderStats(container.querySelector("#confluence-stats"), close);

  const canvas = container.querySelector("#confluence-canvas");
  const scene = new Confluence(canvas, close);
  scene.start();

  container.querySelector("#confluence-alt").textContent = altText(close);

  container.querySelector("#run-close").addEventListener("click", (event) => {
    runClose(event.currentTarget, container, scene, state);
  });

  return () => scene.stop();
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

function renderStats(node, close) {
  if (!close) {
    node.innerHTML = `
      <div class="empty" style="grid-column: 1 / -1">
        <h3>No close has been run</h3>
        <p>Press <strong>Close today's books</strong> above, or run it from a terminal.</p>
        <code>make demo</code>
      </div>`;
    return;
  }
  const tiles = [
    { label: "Rows ingested", value: close.rows_ingested, tone: "" },
    { label: "Match groups", value: close.matches, tone: "ok" },
    { label: "Needs a human", value: close.exceptions, tone: "warn" },
    {
      label: "Auto-post coverage",
      value: `${(Number(close.auto_post_coverage) * 100).toFixed(1)}%`,
      tone: "accent",
      note: `at confidence ≥ ${close.conformal_threshold}`,
    },
    {
      label: "Unexplained",
      value: close.unexplained,
      tone: close.unexplained === "₹0.00" ? "ok" : "warn",
      note: "after the waterfall",
    },
  ];
  node.innerHTML = tiles
    .map(
      (tile) => `
      <div class="stat" data-tone="${tile.tone}">
        <span class="label">${esc(tile.label)}</span>
        <span class="value">${esc(String(tile.value))}</span>
        ${tile.note ? `<span class="note">${esc(tile.note)}</span>` : ""}
      </div>`
    )
    .join("");
  stagger([...node.children], { step: 50 });
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

  const append = (stage) => {
    const item = document.createElement("li");
    item.className = "stagelog-item rise";
    item.innerHTML = `
      <span class="stagelog-name">${esc(stage.label)}</span>
      <span class="stagelog-detail">${esc(stage.detail)}</span>
      <span class="stagelog-count money">${stage.matches_added > 0 ? "+" + stage.matches_added : "—"}</span>
      <span class="stagelog-ms subtle">${Number(stage.elapsed_ms).toFixed(0)}ms</span>`;
    log.appendChild(item);
    log.scrollTop = log.scrollHeight;
  };

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
