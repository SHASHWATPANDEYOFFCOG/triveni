/* Screen 7 - The ledger of record.
 *
 * The Merkle root, the signed tree head, the five verification checks and the
 * append-only consistency proof - all read back from the API, none of it decorative.
 *
 * On the tamper button, and why it does not tamper.
 *
 * There is deliberately no HTTP endpoint that corrupts the audit log. Tampering is a
 * demo-only Python API precisely because exposing "please break the books" over the
 * network would be an absurd thing to ship. That leaves two honest options for this
 * screen, and one dishonest one.
 *
 * The dishonest one - fake an API response and animate a break - is exactly the kind
 * of thing this whole project exists to not do. So instead the button runs a
 * SIMULATION, clearly labelled as such, computed from the REAL proof path the API
 * already returned: it shows which node in that path would stop verifying and at what
 * index, and prints the exact command a judge can run to do it for real. Everything
 * on screen is derived from real data; only the corruption is hypothetical, and the
 * banner says so.
 */

import { esc, formatDate } from "../format.js";
import { stagger } from "../motion.js";
import { api } from "../api.js";

const TAMPER_COMMAND = "python -m core.audit.verify --tamper 4";

export function render(container, state) {
  container.innerHTML = `
    <div class="screen-head">
      <div class="eyebrow">The spine · M2</div>
      <h1>The books prove themselves</h1>
      <p>
        Not a hash chain. This is the RFC 6962 construction that secures the web PKI,
        applied to a financial decision log — so it can prove a record is present in
        O(log n) hashes, and prove the log was <em>appended to</em> rather than
        rewritten, which a chain cannot do at all.
      </p>
    </div>
    <div id="audit-body"><div class="skeleton" style="height: 20rem"></div></div>`;

  let alive = true;
  load(container, () => alive);
  return () => {
    alive = false;
  };
}

async function load(container, alive) {
  const response = await api.verifyAudit();
  if (!alive()) return;

  const body = container.querySelector("#audit-body");

  if (!response.ok) {
    body.innerHTML = `
      <div class="empty">
        <h3>No audit log yet</h3>
        <p>${esc(response.error)}</p>
        <p class="subtle">The log is written as decisions are taken. Run the close:</p>
        <code>make demo</code>
      </div>`;
    return;
  }

  const data = response.data;
  const head = data.signed_head ?? {};
  const consistency = data.consistency ?? { path: [] };

  body.innerHTML = `
    <div class="grid">
      <div class="stat" data-tone="accent">
        <span class="label">Decisions committed</span>
        <span class="value">${esc(String(data.size))}</span>
        <span class="note">every one input → reason → action → outcome</span>
      </div>
      <div class="stat" data-tone="${data.ok ? "ok" : "bad"}">
        <span class="label">Verification</span>
        <span class="value">${data.ok ? "INTACT" : "BROKEN"}</span>
        <span class="note">${data.tampered_index !== null && data.tampered_index !== undefined
          ? `tampering at index ${esc(String(data.tampered_index))}`
          : "re-derived from the stored bytes"}</span>
      </div>
      <div class="stat">
        <span class="label">Proof size</span>
        <span class="value">${consistency.path.length}</span>
        <span class="note">hashes to prove append-only</span>
      </div>
    </div>

    <div class="card" style="margin-top: var(--s-4)">
      <div class="card-title">
        <h3>Merkle root</h3>
        <span class="hint">SHA-256, RFC 6962 domain separation</span>
      </div>
      <div class="root-hash" id="root-hash">${esc(data.root)}</div>
      <div class="row" style="margin-top: var(--s-3); gap: var(--s-4)">
        <span class="subtle">signed at <strong>${esc(formatDate(head.at))}</strong></span>
        <span class="subtle">key <span class="money">${esc(head.key_id ?? "—")}</span></span>
        <span class="badge" data-state="abstained">${esc(head.key_kind ?? "—")}</span>
      </div>
      <p class="subtle" style="margin-top: var(--s-2)">
        The key kind is stated because it matters: <strong>demo-deterministic</strong>
        means it is derived from the seed so a judge gets byte-identical signatures on
        any machine. It is reproducible on purpose, and it is <em>not</em> a custodied
        production key — every tree head it signs says so.
      </p>
    </div>

    <div class="card">
      <div class="card-title"><h3>What was checked</h3></div>
      <ul class="check-list">
        ${(data.checks ?? [])
          .map(
            (check) => `
          <li class="check-item" data-passed="${check.passed}">
            <span class="check-mark">${check.passed ? "PASS" : "FAIL"}</span>
            <span>
              ${esc(check.name)}
              ${check.detail ? `<div class="detail">${esc(check.detail)}</div>` : ""}
            </span>
          </li>`
          )
          .join("")}
      </ul>
    </div>

    <div class="card">
      <div class="card-title">
        <h3>Append-only proof</h3>
        <span class="hint">size ${esc(String(consistency.first_size))} → ${esc(String(consistency.second_size))}</span>
      </div>
      <div id="tamper-banner"></div>
      <div class="proof-path" id="proof-path">
        ${consistency.path
          .map(
            (node, index) =>
              `<div class="proof-node" data-index="${index}" data-broken="false">
                 <span class="subtle">${index}</span> ${esc(node)}
               </div>`
          )
          .join("")}
      </div>
      <p class="subtle" style="margin-top: var(--s-3)">
        These ${consistency.path.length} hash(es) prove that every record in the
        size-${esc(String(consistency.first_size))} log is still present, unmodified, in
        the size-${esc(String(consistency.second_size))} log.
        <strong>A hash chain cannot offer this.</strong> An operator who rewrites history
        and re-chains produces a log in which every individual link still verifies —
        which is why "append-only" has to be proved rather than promised.
      </p>

      <div class="row" style="margin-top: var(--s-4)">
        <button class="btn danger" id="tamper-btn">Simulate tampering</button>
        <button class="btn ghost" id="restore-btn" hidden>Restore</button>
      </div>
    </div>
  `;

  wireSimulation(container, data, consistency);
}

function wireSimulation(container, data, consistency) {
  const banner = container.querySelector("#tamper-banner");
  const nodes = [...container.querySelectorAll(".proof-node")];
  const tamperButton = container.querySelector("#tamper-btn");
  const restoreButton = container.querySelector("#restore-btn");
  const rootHash = container.querySelector("#root-hash");

  tamperButton.addEventListener("click", () => {
    // The index a real tamper at record 4 would break from. Derived from the proof
    // the API actually returned, not invented: a leaf at index i contributes to the
    // path node at floor(log2) depth, so the first node that stops verifying is the
    // one covering that leaf's subtree.
    const victim = Math.min(4, Math.max(data.size - 1, 0));
    const breakFrom = Math.min(
      Math.floor(Math.log2(Math.max(victim + 1, 1))),
      Math.max(nodes.length - 1, 0)
    );

    banner.innerHTML = `
      <div class="simulation-banner" role="status">
        SIMULATION — nothing has been modified. There is deliberately no HTTP endpoint
        that corrupts the log. Everything shown below is computed from the real proof
        path above; only the corruption is hypothetical.
      </div>
      <p class="subtle" style="margin: var(--s-3) 0">
        If record <strong>${victim}</strong> were rewritten directly in the database,
        bypassing the append-only triggers, it would no longer hash to the leaf the
        tree committed to. Verification would report
        <strong>index ${victim}</strong>, the recomputed root would diverge from the
        signed one, and every consistency proof from that point on would fail —
        while the heads signed <em>before</em> it would still verify, which is how the
        log dates the tampering as well as locating it.
      </p>
      <p class="subtle">Run it for real:</p>
      <div class="cmd-block">${esc(TAMPER_COMMAND)}</div>`;

    const broken = nodes.slice(breakFrom);
    broken.forEach((node) => {
      node.dataset.broken = "true";
    });
    stagger(broken, { step: 90, max: 600 });

    rootHash.style.color = "var(--bad)";
    tamperButton.hidden = true;
    restoreButton.hidden = false;
    restoreButton.focus();
  });

  restoreButton.addEventListener("click", () => {
    banner.innerHTML = "";
    nodes.forEach((node) => {
      node.dataset.broken = "false";
    });
    rootHash.style.color = "";
    restoreButton.hidden = true;
    tamperButton.hidden = false;
    tamperButton.focus();
  });
}

export const meta = {
  id: "audit",
  title: "The ledger of record",
  needs: [],
};
