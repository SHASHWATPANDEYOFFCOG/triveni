/* The shell: routing, theme, keyboard, and the one place data is loaded.
 *
 * Screens are plain modules exporting `render(container, state)` and optionally
 * returning a cleanup function. They receive data and never fetch it themselves,
 * which is what keeps them testable, keeps the loading states in one place, and
 * makes it impossible for two screens to disagree about the same number.
 */

import { api } from "./api.js";
import { esc } from "./format.js";
import { onMotionPreferenceChange } from "./motion.js";

const SCREENS = {
  confluence: () => import("./screens/confluence.js"),
  stages: () => import("./screens/stages.js"),
  alpha: () => import("./screens/alpha.js"),
  triage: () => import("./screens/triage.js"),
  waterfall: () => import("./screens/waterfall.js"),
  forecast: () => import("./screens/forecast.js"),
  audit: () => import("./screens/audit.js"),
  report: () => import("./screens/report.js"),
};

const SHORTCUTS = [
  ["1 – 8", "jump to a screen"],
  ["j / k", "move down / up in a list"],
  ["a", "accept the selected exception"],
  ["r", "reject the selected exception"],
  ["e", "expand the evidence bundle"],
  ["t", "switch theme"],
  ["?", "this help"],
  ["Esc", "close a dialog"],
];

const state = {
  date: "2026-03-31",
  alpha: "0.01",
  close: null,
  exceptions: [],
  boundaries: null,
  costmodel: null,
  health: null,
  loaded: false,
  error: "",
};

const cleanups = new Map();
let current = "confluence";

/* --------------------------------------------------------------------------- */
/* Boot                                                                         */
/* --------------------------------------------------------------------------- */
async function boot() {
  wireTheme();
  wireNav();
  wireKeyboard();
  wireHelp();

  showScreen(location.hash.slice(1) || "confluence", { replace: true });
  await load();
}

async function load() {
  const [health, close, exceptions, boundaries, costmodel] = await Promise.all([
    api.health(),
    api.close(state.date, state.alpha),
    api.exceptions({ limit: 200 }),
    api.boundaries(),
    api.costmodel(),
  ]);

  state.health = health.ok ? health.data : null;
  state.close = close.ok ? close.data : null;
  state.exceptions = exceptions.ok ? exceptions.data : [];
  state.boundaries = boundaries.ok ? boundaries.data : null;
  state.costmodel = costmodel.ok ? costmodel.data : null;
  state.loaded = true;
  state.error = close.ok ? "" : close.error;

  renderConnection();
  await showScreen(current, { force: true });
}

function renderConnection() {
  const node = document.getElementById("connection");
  if (!state.error) {
    node.hidden = true;
    return;
  }
  node.hidden = false;
  node.className = "empty";
  node.innerHTML = `
    <h3>The Triveni API is not answering</h3>
    <p>${esc(state.error)}</p>
    <p class="subtle">Every screen below needs it. Start it with:</p>
    <code>make run</code>
    <p class="subtle" style="margin-top: var(--s-3)">
      Or run the whole narrative offline, with no server and no keys:
    </p>
    <code>make demo</code>`;
}

/* --------------------------------------------------------------------------- */
/* Routing                                                                      */
/* --------------------------------------------------------------------------- */
async function showScreen(name, { replace = false, force = false } = {}) {
  if (!SCREENS[name]) name = "confluence";
  if (name === current && !force && cleanups.has(name)) return;

  cleanups.get(current)?.();
  cleanups.delete(current);
  current = name;

  document.querySelectorAll(".nav button").forEach((button) => {
    const active = button.dataset.screen === name;
    button.toggleAttribute("aria-current", active);
    if (active) button.setAttribute("aria-current", "page");
  });

  document.querySelectorAll(".screen").forEach((section) => {
    section.dataset.active = String(section.dataset.screen === name);
  });

  const container = document.querySelector(`.screen[data-screen="${name}"]`);
  if (!state.loaded) {
    container.innerHTML = skeleton();
    return;
  }

  try {
    const module = await SCREENS[name]();
    const cleanup = module.render(container, state, { reload: load, showScreen });
    if (typeof cleanup === "function") cleanups.set(name, cleanup);
  } catch (cause) {
    container.innerHTML = `
      <div class="empty">
        <h3>This screen failed to load</h3>
        <p>${esc(String(cause?.message ?? cause))}</p>
      </div>`;
  }

  if (replace) history.replaceState(null, "", `#${name}`);
  else if (location.hash.slice(1) !== name) history.pushState(null, "", `#${name}`);
}

function skeleton() {
  return `
    <div class="screen-head">
      <div class="skeleton" style="width: 8rem; height: 0.8rem; margin-bottom: var(--s-2)"></div>
      <div class="skeleton" style="width: 22rem; height: 2rem"></div>
    </div>
    <div class="grid">
      ${Array.from({ length: 4 })
        .map(() => '<div class="skeleton" style="height: 5.5rem"></div>')
        .join("")}
    </div>
    <div class="skeleton" style="height: 16rem; margin-top: var(--s-4)"></div>`;
}

function wireNav() {
  document.querySelectorAll(".nav button").forEach((button) => {
    button.addEventListener("click", () => showScreen(button.dataset.screen));
  });
  window.addEventListener("hashchange", () => showScreen(location.hash.slice(1)));
}

/* --------------------------------------------------------------------------- */
/* Theme                                                                        */
/* --------------------------------------------------------------------------- */
function wireTheme() {
  const toggle = document.getElementById("theme-toggle");
  const icon = document.getElementById("theme-icon");

  const paint = () => {
    const explicit = document.documentElement.dataset.theme;
    const dark =
      explicit === "dark" ||
      (!explicit && window.matchMedia("(prefers-color-scheme: dark)").matches);
    icon.textContent = dark ? "☾" : "☀";
    toggle.setAttribute(
      "aria-label",
      dark ? "Switch to the light theme" : "Switch to the dark theme"
    );
  };

  toggle.addEventListener("click", () => {
    const explicit = document.documentElement.dataset.theme;
    const dark =
      explicit === "dark" ||
      (!explicit && window.matchMedia("(prefers-color-scheme: dark)").matches);
    const next = dark ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("triveni-theme", next);
    } catch {
      /* private mode: the choice simply does not persist */
    }
    paint();
  });

  paint();
  onMotionPreferenceChange(() => showScreen(current, { force: true }));
}

/* --------------------------------------------------------------------------- */
/* Keyboard                                                                     */
/* --------------------------------------------------------------------------- */
function wireKeyboard() {
  const order = Object.keys(SCREENS);
  document.addEventListener("keydown", (event) => {
    const target = event.target;
    const typing =
      target instanceof HTMLInputElement ||
      target instanceof HTMLTextAreaElement ||
      target?.isContentEditable;
    if (typing || event.metaKey || event.ctrlKey || event.altKey) return;

    if (event.key >= "1" && event.key <= "8") {
      const name = order[Number(event.key) - 1];
      if (name) {
        event.preventDefault();
        showScreen(name);
      }
      return;
    }
    if (event.key === "t") {
      document.getElementById("theme-toggle").click();
      return;
    }
    if (event.key === "?") {
      event.preventDefault();
      toggleHelp();
      return;
    }
    if (event.key === "Escape") closeHelp();
  });
}

/* --------------------------------------------------------------------------- */
/* Help                                                                         */
/* --------------------------------------------------------------------------- */
function wireHelp() {
  const overlay = document.getElementById("help");
  overlay.innerHTML = `
    <div class="help-panel">
      <div class="card-title">
        <h3>Keyboard</h3>
        <button class="btn ghost icon-btn" id="help-close" aria-label="Close">✕</button>
      </div>
      <dl class="help-list">
        ${SHORTCUTS.map(
          ([key, what]) =>
            `<div><dt><span class="kbd">${esc(key)}</span></dt><dd>${esc(what)}</dd></div>`
        ).join("")}
      </dl>
      <p class="subtle" style="margin-top: var(--s-4)">
        Every action in Triveni is reachable without a mouse.
      </p>
    </div>`;
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay || event.target.id === "help-close") closeHelp();
  });
  document.getElementById("help-toggle").addEventListener("click", toggleHelp);
}

function toggleHelp() {
  const overlay = document.getElementById("help");
  if (overlay.hidden) {
    overlay.hidden = false;
    overlay.querySelector("#help-close")?.focus();
  } else {
    closeHelp();
  }
}

function closeHelp() {
  const overlay = document.getElementById("help");
  if (overlay.hidden) return;
  overlay.hidden = true;
  document.getElementById("help-toggle").focus();
}

boot();
