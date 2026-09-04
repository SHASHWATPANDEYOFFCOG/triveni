/* The command palette.
 *
 * Ctrl/Cmd-K. Navigate, act, and search - the three things a keyboard-first user of a
 * finance tool actually wants, without learning eight separate shortcuts.
 *
 * The matcher is a subsequence match, not a substring one: "sttl" finds "Settlement"
 * because every letter appears in order. Substring matching is what makes most
 * in-house palettes feel broken, since it fails on exactly the abbreviations people
 * naturally type. Results are ranked by how tightly the letters cluster, so an exact
 * prefix beats a scattered match.
 */

import { openDialog } from "./dialog.js";

/**
 * Score `query` against `text`. Returns null for no match, otherwise a number where
 * LOWER is better - it is the span of text the match had to cover, so a tight hit
 * near the front wins.
 */
export function score(query, text) {
  const needle = query.trim().toLowerCase();
  if (!needle) return 0;
  const hay = text.toLowerCase();

  let start = -1;
  let index = 0;
  for (const char of needle) {
    index = hay.indexOf(char, index);
    if (index === -1) return null;
    if (start === -1) start = index;
    index += 1;
  }
  // Span covered, plus how late the match started. Both smaller-is-better.
  return index - start + start * 0.5;
}

export function rank(query, items) {
  return items
    .map((item) => ({ item, s: score(query, `${item.label} ${item.hint ?? ""}`) }))
    .filter((row) => row.s !== null)
    .sort((a, b) => a.s - b.s)
    .map((row) => row.item);
}

export function openPalette(commands) {
  const root = document.getElementById("palette");
  if (!root) return;

  root.innerHTML = `
    <div class="palette-panel">
      <input
        class="palette-input"
        id="palette-input"
        type="text"
        placeholder="Search or jump to…"
        aria-label="Command"
        autocomplete="off"
        spellcheck="false"
      />
      <ul class="palette-list" id="palette-list" role="listbox" aria-label="Commands"></ul>
    </div>`;

  const input = root.querySelector("#palette-input");
  const list = root.querySelector("#palette-list");
  let shown = commands;
  let active = 0;

  function paint() {
    if (!shown.length) {
      list.innerHTML = `<li class="palette-empty">Nothing matches that.</li>`;
      return;
    }
    list.innerHTML = shown
      .map(
        (command, index) => `
        <li role="none">
          <button
            class="palette-item"
            role="option"
            id="palette-opt-${index}"
            aria-selected="${index === active}"
            data-active="${index === active}"
            data-index="${index}"
          >
            <span aria-hidden="true">${command.glyph ?? "›"}</span>
            <span class="what">${escapeHtml(command.label)}</span>
            <span class="where">${escapeHtml(command.hint ?? "")}</span>
          </button>
        </li>`
      )
      .join("");
    input.setAttribute("aria-activedescendant", `palette-opt-${active}`);
    // Keep the keyboard selection in view without scrolling the page behind it.
    list.querySelector('[data-active="true"]')?.scrollIntoView({ block: "nearest" });
  }

  function run(index) {
    const command = shown[index];
    if (!command) return;
    // Close BEFORE running: a command that opens another dialog must not have this
    // one's focus restore fire afterwards and steal focus back.
    close();
    command.run();
  }

  input.addEventListener("input", () => {
    shown = rank(input.value, commands);
    active = 0;
    paint();
  });

  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || (event.key === "n" && event.ctrlKey)) {
      event.preventDefault();
      active = shown.length ? (active + 1) % shown.length : 0;
      paint();
    } else if (event.key === "ArrowUp" || (event.key === "p" && event.ctrlKey)) {
      event.preventDefault();
      active = shown.length ? (active - 1 + shown.length) % shown.length : 0;
      paint();
    } else if (event.key === "Enter") {
      event.preventDefault();
      run(active);
    }
  });

  list.addEventListener("click", (event) => {
    const button = event.target.closest(".palette-item");
    if (button) run(Number(button.dataset.index));
  });

  paint();
  const close = openDialog(root, { initialFocus: "#palette-input" });
}

function escapeHtml(value) {
  return String(value).replace(
    /[&<>"']/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]
  );
}
