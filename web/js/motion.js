/* The motion system.
 *
 * Three rules, and they are why this file exists rather than a scattering of CSS
 * transitions:
 *
 * 1. **Nothing animates that a user asked not to.** `reduced()` is consulted by every
 *    helper here, and when it is true each one jumps straight to the final state. The
 *    end state is always reached - reduced motion removes movement, never content.
 * 2. **Numbers animate on a spring, not a linear tween.** A rupee figure counting up
 *    linearly reads like a slot machine; an eased one reads like a total settling.
 * 3. **Motion is bound to data, not to time.** The odometers and the hero take their
 *    values from the API. Nothing here invents a number to look busy.
 */

const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)");

export function reduced() {
  return REDUCED.matches;
}

export function onMotionPreferenceChange(handler) {
  REDUCED.addEventListener("change", handler);
}

/** Ease-out cubic. Matches --ease closely enough for JS-driven values. */
function easeOut(t) {
  return 1 - Math.pow(1 - t, 3);
}

/**
 * Count from `from` to `to`, calling `render(value)` each frame.
 * Returns a cancel function. Under reduced motion, renders the final value once.
 */
export function animateValue(from, to, duration, render) {
  if (reduced() || duration <= 0) {
    render(to);
    return () => {};
  }
  let raf = 0;
  const start = performance.now();
  const delta = to - from;

  const step = (now) => {
    const progress = Math.min((now - start) / duration, 1);
    render(from + delta * easeOut(progress));
    if (progress < 1) raf = requestAnimationFrame(step);
  };
  raf = requestAnimationFrame(step);
  return () => cancelAnimationFrame(raf);
}

/** An odometer over an element's text content. */
export function odometer(element, to, formatter, duration = 900) {
  const from = Number(element.dataset.value ?? 0);
  element.dataset.value = String(to);
  return animateValue(from, to, duration, (value) => {
    element.textContent = formatter(value);
  });
}

/**
 * Stagger an entrance across a list of elements.
 *
 * The stagger is capped rather than proportional: with 90 exception rows a 40ms
 * step would take three and a half seconds to finish, and a list that is still
 * arriving when you start reading it is worse than one that simply appeared.
 */
export function stagger(elements, { step = 40, max = 320, className = "rise" } = {}) {
  if (reduced()) return;
  elements.forEach((element, index) => {
    element.style.animationDelay = `${Math.min(index * step, max)}ms`;
    element.classList.add(className);
  });
}

/** Fade+lift an element out, then remove it. Used by the triage inbox. */
export function dismiss(element, direction = 1) {
  if (reduced()) {
    element.remove();
    return Promise.resolve();
  }
  const animation = element.animate(
    [
      { opacity: 1, transform: "none", height: `${element.offsetHeight}px` },
      { opacity: 0, transform: `translateX(${direction * 24}px)`, height: "0px" },
    ],
    { duration: 220, easing: "cubic-bezier(.2,.8,.2,1)", fill: "forwards" }
  );
  return animation.finished.then(() => element.remove());
}

/** Draw an SVG path on. Returns immediately under reduced motion. */
export function drawPath(path, duration = 700) {
  if (reduced()) return;
  const length = path.getTotalLength();
  path.style.strokeDasharray = String(length);
  path.style.strokeDashoffset = String(length);
  path.animate([{ strokeDashoffset: length }, { strokeDashoffset: 0 }], {
    duration,
    easing: "cubic-bezier(.16,1,.3,1)",
    fill: "forwards",
  });
}

/** Grow a bar from zero along one axis. */
export function growBar(element, axis = "scaleX", duration = 520, delay = 0) {
  if (reduced()) return;
  element.animate(
    [
      { transform: `${axis}(0)`, opacity: 0.4 },
      { transform: `${axis}(1)`, opacity: 1 },
    ],
    {
      duration,
      delay,
      easing: "cubic-bezier(.2,.8,.2,1)",
      fill: "backwards",
      composite: "replace",
    }
  );
}

/**
 * A frame loop that stops itself when the tab is hidden.
 *
 * The hero canvas runs continuously, and a particle system burning a core in a
 * background tab is the kind of thing that gets a demo closed before it is watched.
 */
export function rafLoop(tick) {
  let raf = 0;
  let last = performance.now();
  let running = true;

  const frame = (now) => {
    if (!running) return;
    const dt = Math.min((now - last) / 16.67, 3);
    last = now;
    tick(dt, now);
    raf = requestAnimationFrame(frame);
  };

  const start = () => {
    if (running) return;
    running = true;
    last = performance.now();
    raf = requestAnimationFrame(frame);
  };
  const stop = () => {
    running = false;
    cancelAnimationFrame(raf);
  };

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stop();
    else start();
  });

  raf = requestAnimationFrame(frame);
  return { stop, start };
}
