/* Scroll reveal, and the counters that run with it.
 *
 * One IntersectionObserver for the whole page rather than one per element, and each
 * element unobserves itself once shown - a reveal that re-runs when you scroll back up
 * is a page that never settles.
 *
 * ---------------------------------------------------------------------------
 * THE RULE THAT MATTERS
 * ---------------------------------------------------------------------------
 * Content is visible by default. `.reveal` sets opacity: 0, and it is applied by
 * script - so if JavaScript fails to load, if the observer never fires, or if the
 * element is inside a container that is never scrolled, the content is simply there.
 *
 * The opposite arrangement - hidden in CSS, revealed by JS - is how a marketing page
 * ends up as a blank column of nothing for anyone whose bundle 404s. That trade is
 * never worth an entrance animation.
 */

const reduced = () =>
  window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;

let observer = null;

/**
 * Reveal every `[data-reveal]` under `root` as it scrolls into view.
 *
 * `data-reveal-delay` (ms) staggers siblings. Keep stagger under ~60ms per item:
 * beyond that a six-card grid takes half a second to finish and reads as slow rather
 * than choreographed.
 */
export function observeReveals(root = document) {
  const targets = [...root.querySelectorAll("[data-reveal]:not([data-shown])")];
  if (!targets.length) return;

  // Reduced motion still reveals - it just skips the movement. Nothing is withheld.
  if (reduced() || !("IntersectionObserver" in window)) {
    for (const el of targets) el.dataset.shown = "true";
    return;
  }

  observer ??= new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const el = entry.target;
        const delay = Number(el.dataset.revealDelay ?? 0);
        setTimeout(() => {
          el.dataset.shown = "true";
          if (el.dataset.count) countUp(el);
        }, delay);
        observer.unobserve(el);
      }
    },
    // A negative bottom margin means "start when it is properly on screen", not the
    // instant one pixel crosses the edge - otherwise everything above the fold has
    // already animated before the page has settled.
    { rootMargin: "0px 0px -12% 0px", threshold: 0.1 }
  );

  for (const el of targets) {
    el.classList.add("reveal");
    observer.observe(el);
  }
}

/**
 * Count an element from 0 to `data-count`.
 *
 * `data-count-decimals` fixes the decimal places, and `data-count-suffix` appends a
 * unit. The final frame writes the target value verbatim rather than a rounded
 * interpolation, because a counter that lands on 95.33% when the number is 95.34% has
 * invented a figure - which is the one thing this product may never do.
 */
export function countUp(el) {
  const target = Number(el.dataset.count);
  if (!Number.isFinite(target)) return;

  const decimals = Number(el.dataset.countDecimals ?? 0);
  const prefix = el.dataset.countPrefix ?? "";
  const suffix = el.dataset.countSuffix ?? "";
  const write = (value) => {
    el.textContent = `${prefix}${value.toFixed(decimals)}${suffix}`;
  };

  if (reduced()) {
    write(target);
    return;
  }

  const duration = Number(el.dataset.countMs ?? 1100);
  const start = performance.now();

  function frame(now) {
    const t = Math.min(1, (now - start) / duration);
    // easeOutExpo: most of the distance covered early, so the number is readable for
    // most of the animation instead of blurring past.
    const eased = t === 1 ? 1 : 1 - Math.pow(2, -10 * t);
    if (t < 1) {
      write(target * eased);
      requestAnimationFrame(frame);
    } else {
      write(target);
    }
  }

  requestAnimationFrame(frame);
}

/** Parallax: translate an element by a fraction of the scroll offset. */
export function observeParallax(root = document) {
  const targets = [...root.querySelectorAll("[data-parallax]")];
  if (!targets.length || reduced()) return () => {};

  let ticking = false;
  function update() {
    ticking = false;
    const height = window.innerHeight;
    for (const el of targets) {
      const box = el.getBoundingClientRect();
      if (box.bottom < 0 || box.top > height) continue;
      // -1..1 across the viewport, 0 when the element is centred.
      const progress = (box.top + box.height / 2 - height / 2) / height;
      const depth = Number(el.dataset.parallax) || 0.1;
      el.style.transform = `translate3d(0, ${(progress * depth * 100).toFixed(2)}px, 0)`;
    }
  }

  function onScroll() {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(update);
  }

  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", onScroll, { passive: true });
  update();

  return () => {
    window.removeEventListener("scroll", onScroll);
    window.removeEventListener("resize", onScroll);
  };
}
