/* Screen 8 - The run report.
 *
 * One page a merchant could print, sign and file. Everything on it is either read
 * back from the API or computed from what the API returned - there is no figure here
 * that a script in the repo cannot regenerate.
 *
 * The footer is the part that matters. It separates what was *measured* from what was
 * *assumed*, by name. The cost model's six parameters are stated assumptions and are
 * labelled as such wherever they appear; the match rate and the realised error are
 * measurements. A report that blurs the two is worse than one that omits the costs
 * entirely, because it launders a guess into a finding.
 */

import { esc, formatPct, humanise } from "../format.js";
import { growBar, stagger } from "../motion.js";
import { api } from "../api.js";

const COMMANDS = [
  ["make setup", "create .venv and install pinned deps — no API keys"],
  ["make demo", "the whole narrative, offline, in under 90 seconds"],
  ["make eval", "→ metrics.json, byte-identical across runs"],
  ["make verify", "re-derive the Merkle root, locate any tampering"],
  ["make redteam", "13 adversarial cases: injection, tampering, ambiguity"],
  ["make bench", "throughput per pipeline stage"],
];

export function render(container, state) {
  const close = state.close;

  if (!close) {
    container.innerHTML = `
      <div class="screen-head">
        <div class="eyebrow">Evidence</div>
        <h1>Run report</h1>
      </div>
      <div class="empty">
        <h3>No run to report</h3>
        <p>The report summarises a close: match rate, exceptions, audit root, boundaries.</p>
        <code>make demo</code>
      </div>`;
    return;
  }

  const byType = tally(state.exceptions ?? [], (item) => item.type);
  const maxType = Math.max(...Object.values(byType), 1);
  const razorpay = state.boundaries?.razorpay;
  const mcp = state.boundaries?.mcp;

  container.innerHTML = `
    <style id="print-rules">
      @media print {
        /* The shell was renamed when the sidebar layout landed (.header/.nav became
         * .rail/.topbar). These selectors were not updated, so the whole navigation
         * chrome printed on every page of the report. */
        .rail, .topbar, .backdrop, .toast-region,
        .guide, .guide-restore, .report-actions { display: none !important; }
        .app { display: block !important; }
        body { background: #fff; color: #000; }
        .card, .stat {
          border: 1px solid #ccc !important;
          box-shadow: none !important;
          break-inside: avoid;
          page-break-inside: avoid;
        }
        .main { padding: 0; max-width: none; }
        a { color: #000; text-decoration: underline; }
      }
    </style>

    <div class="screen-head">
      <div class="eyebrow">Report · ${esc(close.as_of)}</div>
      <h1>Today's close</h1>
      <p>${esc(close.reason)}</p>
      <div class="actionbar report-actions" style="margin-top: var(--s-3)">
        <button class="btn" id="copy-btn">
          <span aria-hidden="true">⧉</span> Copy report
        </button>
        <button class="btn primary" id="print-btn">
          <span aria-hidden="true">⎙</span> Print / Save PDF
        </button>
      </div>
    </div>

    <div class="grid">
      <div class="stat" data-tone="accent">
        <span class="label">Match rate</span>
        <span class="value">${esc(formatPct(Number(close.match_rate), 2))}</span>
        <span class="note">${esc(String(close.matches))} groups over ${esc(String(close.rows_ingested))} rows</span>
      </div>
      <div class="stat" data-tone="ok">
        <span class="label">Posted unsupervised</span>
        <span class="value">${esc(formatPct(Number(close.auto_post_coverage), 1))}</span>
        <span class="note">at confidence ≥ ${esc(String(close.conformal_threshold))}</span>
      </div>
      <div class="stat" data-tone="ok">
        <span class="label">Realised error</span>
        <span class="value">${esc(formatPct(Number(close.realised_error_holdout), 2))}</span>
        <span class="note">held-out, against a ${esc(formatPct(Number(close.nominal_bound), 2))} bound</span>
      </div>
      <div class="stat" data-tone="warn">
        <span class="label">Needs a human</span>
        <span class="value">${esc(String(close.exceptions))}</span>
        <span class="note">typed, evidenced, queued</span>
      </div>
      <div class="stat" data-tone="${close.unexplained === "₹0.00" ? "ok" : "warn"}">
        <span class="label">Unexplained</span>
        <span class="value money">${esc(close.unexplained)}</span>
        <span class="note">after the settlement waterfall</span>
      </div>
    </div>

    <div class="card">
      <div class="card-title">
        <h3>Exceptions by type</h3>
        <span class="hint">a closed 16-value taxonomy — never a free string</span>
      </div>
      <div id="type-bars"></div>
    </div>

    <div class="card">
      <div class="card-title"><h3>Audit root</h3></div>
      <div id="report-audit"><div class="skeleton" style="height: 3rem"></div></div>
    </div>

    <div class="card">
      <div class="card-title">
        <h3>What Triveni may and may not do</h3>
        <span class="hint">generated from the allowlist that enforces it</span>
      </div>
      <p>${esc(razorpay?.summary ?? "boundary unavailable")}</p>
      <p class="subtle">${esc(mcp?.summary ?? "")}</p>
      ${
        mcp?.tools
          ? `<div class="chips" style="margin-top: var(--s-3)">
               ${mcp.tools
                 .map(
                   (tool) =>
                     `<span class="badge" data-state="${tool.mutates ? "needs-review" : "matched"}">
                        ${esc(tool.name)}${tool.mutates ? " · writes" : ""}
                      </span>`
                 )
                 .join("")}
             </div>`
          : ""
      }
    </div>

    <div class="card">
      <div class="card-title">
        <h3>Reproduce every number above</h3>
        <span class="hint">clean clone · no keys · no network</span>
      </div>
      <div class="cmd-block">
${COMMANDS.map(([cmd, why]) => `${esc(cmd.padEnd(16))}<span class="comment"># ${esc(why)}</span>`).join("\n")}
      </div>
    </div>

    ${
      state.boundaries?.adoption_snippet
        ? `<div class="card">
             <div class="card-title">
               <h3>Adopt it in one call</h3>
               <span class="hint">another agent gets a finance controller with a guarantee</span>
             </div>
             <div class="cmd-block"><pre style="margin:0">${esc(state.boundaries.adoption_snippet)}</pre></div>
           </div>`
        : ""
    }

    <div class="honesty">
      <h3 style="font-family: var(--font-ui); font-size: var(--text-base); margin-bottom: var(--s-2)">
        What is measured and what is assumed
      </h3>
      <p>
        <strong>Measured:</strong> the match rate, precision and recall, the fitted
        conformal threshold and the error it realised on a held-out split, the wall
        clock per stage, the Merkle root, and the share of rows that required a model
        call. Every one is regenerated by <code>make eval</code>, which produces a
        byte-identical <code>metrics.json</code> on any machine.
      </p>
      <details class="disclosure" style="margin-bottom: var(--s-3)">
        <summary>Assumed — the six cost parameters</summary>
        <div class="body">
          The loaded cost of an analyst's hour, the minutes to triage an exception, to
          unwind a false match and to review a flagged one, the share of a wrongly-matched
          amount never recovered, and the minutes to eyeball a row by hand. These are
          <em>stated, illustrative assumptions</em>, not sourced findings; they are
          labelled as such in <code>core/costmodel.py</code>, in the eval output and
          here. Substitute your own and every rupee figure moves with them.
        </div>
      </details>
      <p>
        <strong>The data is synthetic.</strong> No real merchant data was used. It is
        generated by <code>data/gen.py</code> from a fixed seed with ground-truth labels,
        and the generator refuses to emit a dataset whose own labels do not balance.
      </p>
    </div>
  `;

  renderTypeBars(container, byType, maxType);
  loadAudit(container);
  container.querySelector("#print-btn").addEventListener("click", () => window.print());

  /* Copy the report as plain text.
   *
   * The button reports its own outcome rather than silently succeeding - clipboard
   * writes fail routinely (an insecure origin, a denied permission, an older engine)
   * and a copy button that does nothing visible is indistinguishable from one that
   * worked. `data-state` drives the spinner/tick styling the button component
   * already has. */
  const copy = container.querySelector("#copy-btn");
  copy.addEventListener("click", async () => {
    const text = (container.innerText ?? container.textContent ?? "").trim();
    const settle = (state, label) => {
      copy.dataset.state = state;
      copy.innerHTML = label;
      setTimeout(() => {
        delete copy.dataset.state;
        copy.innerHTML = '<span aria-hidden="true">⧉</span> Copy report';
      }, 2000);
    };

    try {
      await navigator.clipboard.writeText(text);
      settle("success", '<span aria-hidden="true">✓</span> Copied');
    } catch {
      settle("error", '<span aria-hidden="true">!</span> Copy blocked — select and press Ctrl+C');
    }
  });
}

function renderTypeBars(container, byType, max) {
  const node = container.querySelector("#type-bars");
  const entries = Object.entries(byType).sort((a, b) => b[1] - a[1]);

  if (entries.length === 0) {
    node.innerHTML = `<p class="subtle">No exceptions — the books closed clean.</p>`;
    return;
  }

  node.innerHTML = entries
    .map(
      ([type, count]) => `
      <div class="type-bar">
        <span>${esc(humanise(type))}</span>
        <span class="track"><span class="fill" style="width: ${((count / max) * 100).toFixed(1)}%"></span></span>
        <span class="money">${count}</span>
      </div>`
    )
    .join("");

  node.querySelectorAll(".fill").forEach((fill, index) => growBar(fill, "scaleX", 480, index * 55));
  stagger([...node.children], { step: 40 });
}

async function loadAudit(container) {
  const response = await api.verifyAudit();
  const node = container.querySelector("#report-audit");
  if (!node) return;

  if (!response.ok) {
    node.innerHTML = `<p class="subtle">No audit log yet — run <code>make demo</code>.</p>`;
    return;
  }

  const data = response.data;
  node.innerHTML = `
    <div class="root-hash">${esc(data.root)}</div>
    <p class="subtle" style="margin-top: var(--s-2)">
      ${esc(String(data.size))} decision(s), verified ${data.ok ? "intact" : "BROKEN"} ·
      signed with key ${esc(data.signed_head?.key_id ?? "—")}
      (${esc(data.signed_head?.key_kind ?? "—")})
    </p>`;
}

function tally(items, pick) {
  const counts = {};
  for (const item of items) {
    const key = pick(item);
    counts[key] = (counts[key] ?? 0) + 1;
  }
  return counts;
}

export const meta = {
  id: "report",
  title: "Run report",
  needs: ["close", "exceptions", "boundaries"],
};
