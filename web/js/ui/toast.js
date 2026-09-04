/* Toasts.
 *
 * A toast is for something that happened and needs acknowledging but not acting on:
 * "exception accepted", "audit verified", "the API stopped answering". Anything that
 * needs a decision belongs in a dialog, where it cannot time out while the person is
 * reading it.
 *
 * The region is `role="region"` with `aria-live` supplied by the container in
 * index.html, so screen readers announce new toasts without the region itself being
 * a focus stop.
 */

const DEFAULT_MS = 4200;

/** Toast tones map to the same semantic colours as everything else in the product. */
const GLYPHS = {
  success: "✓",
  error: "!",
  warn: "!",
  info: "i",
};

export function toast(message, { tone = "info", ms = DEFAULT_MS, action } = {}) {
  const region = document.getElementById("toasts");
  if (!region) return () => {};

  const node = document.createElement("div");
  node.className = "toast";
  node.dataset.tone = tone;
  node.setAttribute("role", tone === "error" ? "alert" : "status");

  const glyph = document.createElement("span");
  glyph.className = "badge plain";
  glyph.setAttribute("aria-hidden", "true");
  glyph.textContent = GLYPHS[tone] ?? GLYPHS.info;

  const body = document.createElement("div");
  body.className = "grow";
  // textContent, never innerHTML: a toast frequently carries an error string that
  // originated on the server, and that is exactly the path an injected payload takes.
  body.textContent = message;

  node.append(glyph, body);

  if (action) {
    const button = document.createElement("button");
    button.className = "btn sm ghost";
    button.textContent = action.label;
    button.addEventListener("click", () => {
      action.onClick?.();
      dismiss();
    });
    node.append(button);
  }

  const close = document.createElement("button");
  close.className = "btn sm ghost icon-btn";
  close.setAttribute("aria-label", "Dismiss");
  close.textContent = "✕";
  close.addEventListener("click", () => dismiss());
  node.append(close);

  region.append(node);

  let timer = ms > 0 ? setTimeout(dismiss, ms) : null;

  // Hovering or focusing pauses the countdown. A toast that vanishes while you are
  // reaching for its action button is worse than no action button.
  node.addEventListener("pointerenter", () => {
    if (timer) clearTimeout(timer);
    timer = null;
  });
  node.addEventListener("pointerleave", () => {
    if (ms > 0 && !timer) timer = setTimeout(dismiss, ms);
  });

  let gone = false;
  function dismiss() {
    if (gone) return;
    gone = true;
    if (timer) clearTimeout(timer);
    node.dataset.leaving = "true";
    // Wait for the exit animation, but never trust the event to arrive - if the
    // element is display:none'd by a parent, `animationend` never fires and the node
    // would leak. The timeout is the floor.
    const remove = () => node.remove();
    node.addEventListener("animationend", remove, { once: true });
    setTimeout(remove, 400);
  }

  return dismiss;
}
