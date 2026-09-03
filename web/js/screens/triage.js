/* Screen 4 - Exception triage.
 *
 * Keyboard-first, because this is the screen a finance analyst would actually live in
 * and reaching for a mouse eighty-seven times is the difference between a tool people
 * use and one they open once.
 *
 * The field that matters most on every row is `abstained_because`. Anyone can build a
 * queue of things that failed; the useful part is the sentence explaining *why the
 * system stopped* - "attributing it would have created more unexplained money than
 * the payment is worth" tells an analyst in one line whether to look, and that is the
 * whole value of abstaining rather than guessing.
 *
 * Resolving requires both an approver and a reason. The API refuses without them, and
 * so does this form, because a decision with no name on it is not a decision.
 */

import { esc, humanise, truncate } from "../format.js";
import { dismiss, stagger } from "../motion.js";
import { api } from "../api.js";

const SEVERITY_ORDER = ["high", "medium", "low", "info"];

export function render(container, state) {
  const all = Array.isArray(state.exceptions) ? [...state.exceptions] : [];

  if (all.length === 0) {
    container.innerHTML = `
      <div class="screen-head">
        <div class="eyebrow">Loop 1 · Triage</div>
        <h1>Exception inbox</h1>
      </div>
      <div class="empty">
        <h3>Nothing needs a human</h3>
        <p>Either the books closed clean, or no close has run yet.</p>
        <code>make demo</code>
      </div>`;
    return;
  }

  const filters = { severity: "", type: "" };
  let selected = 0;
  let visible = [];

  container.innerHTML = `
    <div class="screen-head">
      <div class="eyebrow">Loop 1 · Triage</div>
      <h1>What needs a person</h1>
      <p>
        Every row here is a typed exception with its evidence attached and a sentence
        saying why the system stopped rather than guessing.
        <span class="kbd">j</span> <span class="kbd">k</span> to move,
        <span class="kbd">a</span> accept, <span class="kbd">r</span> reject,
        <span class="kbd">e</span> expand.
      </p>
    </div>

    <div class="card">
      <div class="card-title">
        <h3>Filter</h3>
        <span class="hint" id="triage-count"></span>
      </div>
      <div class="stack">
        <div class="chips" id="sev-chips" role="group" aria-label="Filter by severity"></div>
        <div class="chips" id="type-chips" role="group" aria-label="Filter by exception type"></div>
      </div>
    </div>

    <div class="card">
      <div class="card-title">
        <h3>Approving as</h3>
        <span class="hint">a decision with no name on it is not a decision</span>
      </div>
      <div class="row">
        <label class="sr-only" for="approver">Approver email</label>
        <input class="qa-input" id="approver" value="priya@merchant.in"
               style="flex:1; min-width:12rem; font:inherit; padding:var(--s-2) var(--s-3);
                      border:1px solid var(--rule-strong); border-radius:var(--r-2);
                      background:var(--bg); color:var(--fg)" />
        <label class="sr-only" for="reason">Reason</label>
        <input id="reason" placeholder="reason (required)"
               style="flex:2; min-width:14rem; font:inherit; padding:var(--s-2) var(--s-3);
                      border:1px solid var(--rule-strong); border-radius:var(--r-2);
                      background:var(--bg); color:var(--fg)" />
      </div>
      <p class="subtle" style="margin-top: var(--s-2)" id="resolve-status" aria-live="polite"></p>
    </div>

    <div class="card" style="padding: 0">
      <ul class="inbox" id="inbox" role="listbox" aria-label="Exceptions" tabindex="0"></ul>
    </div>
  `;

  const inbox = container.querySelector("#inbox");
  const status = container.querySelector("#resolve-status");

  /* ---- filters ---------------------------------------------------------- */
  const severityCounts = tally(all, (item) => item.severity);
  const typeCounts = tally(all, (item) => item.type);

  container.querySelector("#sev-chips").innerHTML =
    chip("", "All severities", all.length, true) +
    SEVERITY_ORDER.filter((key) => severityCounts[key])
      .map((key) => chip(`sev:${key}`, humanise(key), severityCounts[key]))
      .join("");

  container.querySelector("#type-chips").innerHTML =
    chip("", "All types", all.length, true) +
    Object.entries(typeCounts)
      .sort((a, b) => b[1] - a[1])
      .map(([key, count]) => chip(`type:${key}`, humanise(key), count))
      .join("");

  container.querySelectorAll(".chip").forEach((button) => {
    button.addEventListener("click", () => {
      const [kind, value] = (button.dataset.key || ":").split(":");
      const group = button.closest(".chips");
      group.querySelectorAll(".chip").forEach((other) =>
        other.setAttribute("aria-pressed", String(other === button))
      );
      if (group.id === "sev-chips") filters.severity = kind === "sev" ? value : "";
      else filters.type = kind === "type" ? value : "";
      selected = 0;
      paint();
    });
  });

  /* ---- list ------------------------------------------------------------- */
  function paint() {
    visible = all.filter(
      (item) =>
        (!filters.severity || item.severity === filters.severity) &&
        (!filters.type || item.type === filters.type)
    );

    container.querySelector("#triage-count").textContent =
      `${visible.length} of ${all.length} shown`;

    if (visible.length === 0) {
      inbox.innerHTML = `
        <li class="empty" style="border:none">
          <h3>Nothing matches this filter</h3>
          <p>Clear a chip above to widen it.</p>
        </li>`;
      return;
    }

    inbox.innerHTML = visible.map((item, index) => row(item, index)).join("");
    inbox.querySelectorAll(".inbox-row").forEach((node, index) => {
      node.addEventListener("click", () => {
        selected = index;
        highlight();
      });
      node.querySelector(".expand")?.addEventListener("click", (event) => {
        event.stopPropagation();
        toggleEvidence(index);
      });
    });
    stagger([...inbox.children].slice(0, 20), { step: 25 });
    highlight();
  }

  function row(item, index) {
    return `
      <li class="inbox-row" data-index="${index}" data-id="${esc(item.exception_id)}"
          role="option" aria-selected="false">
        <div class="inbox-head">
          <span class="badge" data-sev="${esc(item.severity)}">${esc(humanise(item.type))}</span>
          <span class="inbox-reason">${esc(truncate(item.reason, 96))}</span>
          <span class="money">${esc(item.amount)}</span>
        </div>
        <div class="row" style="margin-top: var(--s-2); gap: var(--s-2)">
          <button class="btn ghost expand" style="font-size: var(--text-xs); padding: 0 var(--s-2)">
            evidence (${item.evidence_count})
          </button>
        </div>
        <div class="evidence" hidden></div>
      </li>`;
  }

  function toggleEvidence(index) {
    const node = inbox.querySelector(`.inbox-row[data-index="${index}"]`);
    if (!node) return;
    const panel = node.querySelector(".evidence");
    const item = visible[index];
    if (!panel.dataset.filled) {
      panel.innerHTML = `
        <dt>Why this row is here</dt>
        <dd>${esc(item.reason)}</dd>
        ${
          item.abstained_because
            ? `<dt>Why the system stopped</dt>
               <dd class="abstained">${esc(item.abstained_because)}</dd>`
            : ""
        }
        <dt>Suggested action</dt>
        <dd>${esc(item.suggested_action || "—")}</dd>
        <dt>Evidence items</dt>
        <dd>${item.evidence_count}</dd>`;
      panel.dataset.filled = "1";
    }
    panel.hidden = !panel.hidden;
  }

  function highlight() {
    inbox.querySelectorAll(".inbox-row").forEach((node, index) => {
      const on = index === selected;
      node.dataset.selected = String(on);
      node.setAttribute("aria-selected", String(on));
      if (on) node.scrollIntoView({ block: "nearest" });
    });
  }

  /* ---- resolving -------------------------------------------------------- */
  async function resolve(decision) {
    const item = visible[selected];
    if (!item) return;

    const approver = container.querySelector("#approver").value.trim();
    const reason = container.querySelector("#reason").value.trim();
    if (!approver || !reason) {
      status.textContent =
        "Both an approver and a reason are required — the API refuses without them, and so does this form.";
      container.querySelector(reason ? "#approver" : "#reason").focus();
      return;
    }

    status.textContent = `recording ${decision}…`;
    const response = await api.resolve(item.exception_id, { decision, reason, approver });

    if (!response.ok) {
      status.textContent = `refused: ${response.error}`;
      return;
    }

    const payload = response.data;
    status.innerHTML =
      `<strong>${esc(decision)}</strong> recorded at audit index ` +
      `<span class="money">${esc(String(payload.audit_index))}</span> — root ` +
      `<span class="money">${esc(String(payload.merkle_root).slice(0, 24))}…</span>. ` +
      `The inclusion proof came back with the response, so you can verify it without ` +
      `trusting this response.`;

    const node = inbox.querySelector(`.inbox-row[data-index="${selected}"]`);
    const position = all.indexOf(item);
    if (position >= 0) all.splice(position, 1);
    if (node) await dismiss(node, decision === "accept" ? 1 : -1);
    selected = Math.min(selected, Math.max(visible.length - 2, 0));
    paint();
  }

  /* ---- keyboard --------------------------------------------------------- */
  const onKey = (event) => {
    const target = event.target;
    const typing =
      target instanceof HTMLInputElement ||
      target instanceof HTMLTextAreaElement ||
      target?.isContentEditable;
    if (typing || event.metaKey || event.ctrlKey || event.altKey) return;
    if (container.dataset.active !== "true" && container.getAttribute("data-active") !== "true") {
      return;
    }

    if (event.key === "j" || event.key === "ArrowDown") {
      event.preventDefault();
      selected = Math.min(selected + 1, visible.length - 1);
      highlight();
    } else if (event.key === "k" || event.key === "ArrowUp") {
      event.preventDefault();
      selected = Math.max(selected - 1, 0);
      highlight();
    } else if (event.key === "e") {
      event.preventDefault();
      toggleEvidence(selected);
    } else if (event.key === "a") {
      event.preventDefault();
      resolve("accept");
    } else if (event.key === "r") {
      event.preventDefault();
      resolve("reject");
    }
  };

  document.addEventListener("keydown", onKey);
  paint();

  return () => document.removeEventListener("keydown", onKey);
}

function chip(key, label, count, pressed = false) {
  return `<button class="chip" data-key="${esc(key)}" aria-pressed="${pressed}">
    ${esc(label)} <span class="subtle">${count}</span>
  </button>`;
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
  id: "triage",
  title: "Exception triage",
  needs: ["exceptions"],
};
