/* The shell: routing, theme, keyboard, navigation, and the one place data is loaded.
 *
 * Screens are plain modules exporting `render(container, state)` and optionally
 * returning a cleanup function. They receive data and never fetch it themselves,
 * which is what keeps them testable, keeps the loading states in one place, and
 * makes it impossible for two screens to disagree about the same number.
 */

import { api } from "./api.js";
import { esc } from "./format.js";
import { onMotionPreferenceChange } from "./motion.js";
import { openDialog } from "./ui/dialog.js";
import { openPalette } from "./ui/palette.js";
import { toast } from "./ui/toast.js";
import { mountGuide, resetGuides } from "./ui/guide.js";
import { observeReveals } from "./motion/reveal.js";

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

const TITLES = {
  confluence: "Confluence",
  stages: "Stages",
  alpha: "α slider",
  triage: "Triage",
  waterfall: "Settlement",
  forecast: "Forecast",
  audit: "Audit",
  report: "Report",
};

const SHORTCUTS = [
  ["1 – 8", "jump to a screen"],
  ["Ctrl K", "command palette"],
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
  closing: false,
  error: "",
};

const cleanups = new Map();
let current = "confluence";
let closeHelpDialog = null;

/* --------------------------------------------------------------------------- */
/* Boot                                                                         */
/* --------------------------------------------------------------------------- */
async function boot() {
  wireTheme();
  wireNav();
  wireRail();
  wireKeyboard();
  wireHelp();
  wirePalette();

  showScreen(location.hash.slice(1) || "confluence", { replace: true });
  await load();
}

async function load() {
  // Two waves, on purpose.
  //
  // /health is the only endpoint that needs no reconciliation, so it is the whole of
  // wave one. Everything else - close, exceptions, boundaries, cost model - reads the
  // same reconciliation, and a cold one is about ten seconds: Fellegi-Sunter and the
  // global solver are ~5s each, the honest price of solving the whole day at once.
  // The server warms it at startup, so in practice wave two is usually already paid.
  //
  // Awaiting them together meant the page showed nothing but skeletons and read as
  // broken. Now the shell paints immediately and a banner says what is still running.
  const health = await api.health();
  state.health = health.ok ? health.data : null;
  state.loaded = true;
  state.error = health.ok ? "" : health.error;

  renderConnection();
  renderMode();
  setClosing(true);
  await showScreen(current, { force: true });

  const [close, exceptions, boundaries, costmodel] = await Promise.all([
    api.close(state.date, state.alpha),
    api.exceptions({ limit: 200 }),
    api.boundaries(),
    api.costmodel(),
  ]);

  state.close = close.ok ? close.data : null;
  state.exceptions = exceptions.ok ? exceptions.data : [];
  state.boundaries = boundaries.ok ? boundaries.data : null;
  state.costmodel = costmodel.ok ? costmodel.data : null;
  if (!close.ok) state.error = close.error;
  setClosing(false);

  renderConnection();
  await showScreen(current, { force: true });
}

/** A visible, honest note about the one slow call, instead of a mute skeleton. */
function setClosing(active) {
  state.closing = active;
  let banner = document.getElementById("closing");
  if (!active) {
    banner?.remove();
    return;
  }
  if (banner) return;
  banner = document.createElement("div");
  banner.id = "closing";
  banner.className = "alert";
  banner.dataset.tone = "info";
  banner.setAttribute("role", "status");
  banner.innerHTML = `
    <span class="status-dot" data-live="true" aria-hidden="true"></span>
    <span>
      <strong>Closing the books…</strong>
      reconciling 536 rows across three ledgers. The global solver and Fellegi-Sunter
      are about five seconds each — that is the cost of solving the whole day at once
      rather than greedily. Everything else on this page is already live.
    </span>`;
  document.getElementById("main").prepend(banner);
}

function renderMode() {
  const node = document.getElementById("rail-mode");
  if (!node) return;
  const health = state.health;
  node.textContent = health
    ? `${health.rails} rails · llm ${health.llm_mode}`
    : "API unreachable";
  const dot = document.querySelector("#rail-status .status-dot");
  if (dot) {
    dot.style.background = health ? "var(--ok)" : "var(--bad)";
    dot.dataset.live = String(Boolean(health));
  }
}

function renderConnection() {
  const node = document.getElementById("connection");
  if (!state.error) {
    node.hidden = true;
    return;
  }
  node.hidden = false;
  node.className = "state-block";
  node.dataset.tone = "error";
  node.innerHTML = `
    <span class="glyph" aria-hidden="true">!</span>
    <h3>The Triveni API is not answering</h3>
    <p>${esc(state.error)}</p>
    <p class="faint">Every screen below needs it. Start it with:</p>
    <code>make run</code>
    <p class="faint">Or run the whole narrative offline, with no server and no keys:</p>
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

  document.title = `${TITLES[name] ?? "Triveni"} · Triveni`;
  closeRail();

  const container = document.querySelector(`.screen[data-screen="${name}"]`);
  if (!state.loaded) {
    container.innerHTML = skeleton();
    return;
  }

  try {
    const module = await SCREENS[name]();
    const cleanup = module.render(container, state, { reload: load, showScreen, toast });
    if (typeof cleanup === "function") cleanups.set(name, cleanup);
    // Guidance is mounted by the router rather than by each screen, so every screen
    // gets it in the same position by construction and none can forget to.
    mountGuide(container, name);
    observeReveals(container);
  } catch (cause) {
    container.innerHTML = `
      <div class="state-block" data-tone="error">
        <span class="glyph" aria-hidden="true">!</span>
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
      <div class="skeleton" style="width: 8rem; height: 0.8rem; margin-bottom: var(--space-2)"></div>
      <div class="skeleton" style="width: 22rem; height: 2rem; max-width: 100%"></div>
    </div>
    <div class="grid">
      ${Array.from({ length: 4 })
        .map(() => '<div class="skeleton" style="height: 5.5rem"></div>')
        .join("")}
    </div>
    <div class="skeleton" style="height: 16rem; margin-top: var(--space-4)"></div>`;
}

function wireNav() {
  document.querySelectorAll(".nav button").forEach((button) => {
    button.addEventListener("click", () => showScreen(button.dataset.screen));
  });
  window.addEventListener("hashchange", () => showScreen(location.hash.slice(1)));
}

/* --------------------------------------------------------------------------- */
/* The rail, as a drawer below 64rem                                            */
/* --------------------------------------------------------------------------- */
function wireRail() {
  const toggle = document.getElementById("rail-toggle");
  const scrim = document.getElementById("rail-scrim");
  toggle?.addEventListener("click", () => {
    const app = document.getElementById("app");
    app.dataset.rail === "open" ? closeRail() : openRail();
  });
  scrim?.addEventListener("click", closeRail);
}

function openRail() {
  const app = document.getElementById("app");
  app.dataset.rail = "open";
  document.getElementById("rail-scrim").hidden = false;
  document.getElementById("rail-toggle")?.setAttribute("aria-expanded", "true");
  document.querySelector(".nav button")?.focus();
}

function closeRail() {
  const app = document.getElementById("app");
  if (app.dataset.rail !== "open") return;
  delete app.dataset.rail;
  document.getElementById("rail-scrim").hidden = true;
  document.getElementById("rail-toggle")?.setAttribute("aria-expanded", "false");
}

/* --------------------------------------------------------------------------- */
/* Theme                                                                        */
/* --------------------------------------------------------------------------- */
function isDark() {
  const explicit = document.documentElement.dataset.theme;
  return (
    explicit === "dark" ||
    (!explicit && window.matchMedia("(prefers-color-scheme: dark)").matches)
  );
}

function wireTheme() {
  const toggle = document.getElementById("theme-toggle");
  const icon = document.getElementById("theme-icon");

  const paint = () => {
    const dark = isDark();
    icon.textContent = dark ? "☾" : "☀";
    toggle.setAttribute(
      "aria-label",
      dark ? "Switch to the light theme" : "Switch to the dark theme"
    );
  };

  toggle.addEventListener("click", () => {
    const next = isDark() ? "light" : "dark";
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
    // Ctrl/Cmd-K works even inside a text field - it is the one global that has to.
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      showPalette();
      return;
    }

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
    }
  });
}

/* --------------------------------------------------------------------------- */
/* Command palette                                                              */
/* --------------------------------------------------------------------------- */
function wirePalette() {
  document.getElementById("palette-trigger")?.addEventListener("click", showPalette);
}

function commands() {
  const list = Object.entries(TITLES).map(([key, label], index) => ({
    label,
    hint: `screen · ${index + 1}`,
    glyph: "→",
    run: () => showScreen(key),
  }));

  list.push(
    {
      label: "Reload the close",
      hint: "re-run the reconciliation",
      glyph: "↻",
      run: () => {
        toast("Reloading the close…", { tone: "info" });
        load();
      },
    },
    {
      label: isDark() ? "Switch to the light theme" : "Switch to the dark theme",
      hint: "theme · t",
      glyph: "◐",
      run: () => document.getElementById("theme-toggle").click(),
    },
    {
      label: "Keyboard shortcuts",
      hint: "help · ?",
      glyph: "?",
      run: () => toggleHelp(),
    },
    {
      label: "Open the API documentation",
      hint: "/docs · new tab",
      glyph: "↗",
      run: () => window.open("/docs", "_blank", "noopener"),
    }
  );
  return list;
}

function showPalette() {
  openPalette(commands());
}

/* --------------------------------------------------------------------------- */
/* Help                                                                         */
/* --------------------------------------------------------------------------- */
function wireHelp() {
  document.getElementById("help-toggle").addEventListener("click", toggleHelp);
}

function toggleHelp() {
  if (closeHelpDialog) {
    closeHelpDialog();
    closeHelpDialog = null;
    return;
  }

  const overlay = document.getElementById("help");
  overlay.innerHTML = `
    <div class="modal">
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
      <p class="faint" style="margin-top: var(--space-5)">
        Every action in Triveni is reachable without a mouse.
      </p>
      <button class="btn outline sm" id="help-reset-guides" style="margin-top: var(--space-3)">
        Show the per-screen instructions again
      </button>
    </div>`;

  closeHelpDialog = openDialog(overlay, {
    initialFocus: "#help-close",
    onClose: () => {
      closeHelpDialog = null;
    },
  });

  overlay.querySelector("#help-close").addEventListener("click", () => {
    closeHelpDialog?.();
    closeHelpDialog = null;
  });

  overlay.querySelector("#help-reset-guides").addEventListener("click", () => {
    resetGuides();
    closeHelpDialog?.();
    closeHelpDialog = null;
    toast("Instructions restored on every screen.", { tone: "success" });
    showScreen(current, { force: true });
  });
}

boot();
