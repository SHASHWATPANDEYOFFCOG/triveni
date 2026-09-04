/* Dialog behaviour: focus trapping, restore, Escape, click-outside.
 *
 * Every modal surface in the product goes through here - the help sheet and the
 * command palette today, anything added later by construction. The alternative is
 * each one re-implementing the same four behaviours and getting a different subset
 * of them right, which is how a dialog ends up dismissable by mouse but not by
 * keyboard.
 *
 * What a dialog owes the person using it:
 *
 *   1. Focus moves INTO it when it opens, or a keyboard user is stranded outside a
 *      thing that is covering the page.
 *   2. Tab cannot leave it while it is open.
 *   3. Escape closes it.
 *   4. Focus returns to whatever opened it, so the tab position is not lost.
 *
 * Point 4 is the one usually skipped, and it is the one that makes the difference
 * between a dialog that feels native and one that feels bolted on.
 */

const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled]):not([type='hidden'])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

/** Visible, focusable descendants, in DOM order. */
function focusable(root) {
  return [...root.querySelectorAll(FOCUSABLE)].filter(
    (el) => !el.hasAttribute("hidden") && el.offsetParent !== null
  );
}

/**
 * Open `element` as a modal dialog.
 *
 * Returns a `close()` function. Calling it twice is safe - the second call is a no-op
 * rather than a second focus restore, which would otherwise yank focus away from
 * wherever the user had since moved it.
 */
export function openDialog(element, { onClose, initialFocus } = {}) {
  const opener = document.activeElement;
  let open = true;

  element.hidden = false;

  const target =
    (initialFocus && element.querySelector(initialFocus)) || focusable(element)[0] || element;
  // A dialog whose content is built by the caller may not be laid out yet on the
  // same frame; focusing after a frame is the difference between landing on the
  // close button and landing on nothing.
  requestAnimationFrame(() => target.focus?.());

  function onKeydown(event) {
    if (event.key === "Escape") {
      event.preventDefault();
      close();
      return;
    }
    if (event.key !== "Tab") return;

    const items = focusable(element);
    if (!items.length) {
      event.preventDefault();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    // `document.activeElement` rather than event.target: focus may be on the dialog
    // container itself, which is not in `items`, and the wrap must still work.
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function onPointerDown(event) {
    if (event.target === element) close();
  }

  function close() {
    if (!open) return;
    open = false;
    document.removeEventListener("keydown", onKeydown, true);
    element.removeEventListener("pointerdown", onPointerDown);
    element.hidden = true;
    // Only restore if the opener is still in the document and still focusable -
    // a palette action that navigates away can remove it, and focusing a detached
    // node silently sends focus to <body>, losing the position entirely.
    if (opener?.isConnected) opener.focus?.();
    onClose?.();
  }

  // Capture phase, so Escape reaches us before a screen's own key handler can
  // swallow it.
  document.addEventListener("keydown", onKeydown, true);
  element.addEventListener("pointerdown", onPointerDown);

  return close;
}
