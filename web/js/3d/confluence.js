/* The hero: three ledgers converging into one settlement.
 *
 * ---------------------------------------------------------------------------
 * WHY THIS SHAPE
 * ---------------------------------------------------------------------------
 * The scene is the product. Three streams - payments, the bank statement, the ledger -
 * arrive from three directions along curved paths and meet at a single core. That is
 * exactly what a close is: three sources of truth about the same day, reconciled into
 * one balanced set of books. Particles that reach the core flash and are absorbed.
 *
 * A rotating dodecahedron would have looked equally expensive and said nothing. If the
 * hero visual does not carry the argument, it is decoration with a frame budget.
 *
 * ---------------------------------------------------------------------------
 * PERFORMANCE
 * ---------------------------------------------------------------------------
 * A hero animation that costs a phone its battery is a bad trade for a first
 * impression, so the loop stops whenever it is not being looked at:
 *
 *   - off-screen                  -> IntersectionObserver pauses the rAF loop
 *   - tab hidden                  -> visibilitychange pauses it
 *   - prefers-reduced-motion      -> ONE frame is composed, then nothing
 *   - coarse pointer / few cores  -> particle count drops, blur is dropped
 *
 * Everything drawn is a filled arc or a stroked path. There is no per-frame
 * allocation in the draw path and no shadow blur on the particle pass - `shadowBlur`
 * on a few hundred sprites is, on its own, the difference between 60fps and 20.
 */

import { bezier, damp, depthSort, project, rng, rotateX, rotateY } from "./engine.js";

const STREAMS = [
  { key: "payments", label: "Payments", hue: "--stream-payments", from: { x: -560, y: -300, z: 420 } },
  { key: "bank", label: "Bank", hue: "--stream-bank", from: { x: 600, y: -180, z: 300 } },
  { key: "ledger", label: "Ledger", hue: "--stream-ledger", from: { x: -60, y: 420, z: 620 } },
];

const CORE = { x: 0, y: 0, z: 0 };

/** Fewer particles where the GPU is likely to be small or the screen is a phone. */
function budget() {
  const cores = navigator.hardwareConcurrency ?? 4;
  const coarse = window.matchMedia?.("(pointer: coarse)").matches;
  const narrow = window.innerWidth < 760;
  if (coarse || narrow || cores <= 4) return { perStream: 26, trail: 34, glow: false };
  if (cores <= 8) return { perStream: 44, trail: 52, glow: true };
  return { perStream: 62, trail: 64, glow: true };
}

export function mountConfluence(canvas) {
  const context = canvas.getContext?.("2d");
  // No 2D context means a very old or very locked-down browser. The container keeps
  // its CSS gradient, which is a static version of this composition, and nothing
  // here runs. It still looks deliberate; it just does not move.
  if (!context) return () => {};

  const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
  const config = budget();
  const random = rng(20260101);

  const styles = getComputedStyle(document.documentElement);
  const colour = (name, fallback) => styles.getPropertyValue(name).trim() || fallback;

  const palette = {
    payments: colour("--stream-payments", "#5b8cff"),
    bank: colour("--stream-bank", "#a78bfa"),
    ledger: colour("--stream-ledger", "#22d3ee"),
    core: colour("--stream-core", "#f8fafc"),
  };

  // Each stream is a cubic Bezier from its origin to the core. The two control points
  // bend the path so the three approaches curve in rather than arriving as spokes -
  // a confluence, not an asterisk.
  const paths = STREAMS.map((stream, index) => ({
    ...stream,
    colour: palette[stream.key],
    c1: {
      x: stream.from.x * 0.45,
      y: stream.from.y * 0.9 + (index - 1) * 120,
      z: stream.from.z * 0.8,
    },
    c2: { x: stream.from.x * 0.12, y: stream.from.y * 0.1, z: stream.from.z * 0.25 },
    particles: Array.from({ length: config.perStream }, () => ({
      t: random(),
      speed: 0.0016 + random() * 0.0026,
      spread: (random() - 0.5) * 74,
      spreadY: (random() - 0.5) * 74,
      size: 0.9 + random() * 1.9,
    })),
  }));

  let width = 0;
  let height = 0;
  let cx = 0;
  let cy = 0;
  let focal = 900;

  function resize() {
    const box = canvas.getBoundingClientRect();
    // Cap the device pixel ratio at 2. A 3x phone paints 9x the pixels of a 1x screen
    // for a difference nobody can see on a soft-edged particle.
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    width = Math.max(1, box.width);
    height = Math.max(1, box.height);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    cx = width / 2;
    cy = height / 2;
    // Focal length scales with the canvas so the composition frames identically at
    // 390px and 2560px instead of the scene falling out of the viewport on a phone.
    focal = Math.max(520, Math.min(width, height) * 1.35);
  }

  /* --- interaction -------------------------------------------------------- */
  let targetYaw = 0;
  let targetPitch = 0;
  let yaw = 0;
  let pitch = 0;

  function onPointerMove(event) {
    const box = canvas.getBoundingClientRect();
    // -1..1 from the centre of the canvas.
    const nx = (event.clientX - box.left) / box.width - 0.5;
    const ny = (event.clientY - box.top) / box.height - 0.5;
    // Deliberately shallow: +-0.34rad is about 20 degrees. A hero that spins to face
    // the cursor is a toy; a hero that leans is a window.
    targetYaw = nx * 0.68;
    targetPitch = -ny * 0.4;
  }

  function onPointerLeave() {
    targetYaw = 0;
    targetPitch = 0;
  }

  /* --- draw --------------------------------------------------------------- */
  function view(point) {
    return rotateX(rotateY(point, yaw), pitch);
  }

  function drawPath(path) {
    context.beginPath();
    let started = false;
    for (let i = 0; i <= 48; i += 1) {
      const point = project(view(bezier(path.from, path.c1, path.c2, CORE, i / 48)), {
        focal,
        cx,
        cy,
      });
      if (!point.visible) {
        started = false;
        continue;
      }
      started ? context.lineTo(point.x, point.y) : context.moveTo(point.x, point.y);
      started = true;
    }
    context.strokeStyle = path.colour;
    context.globalAlpha = 0.16;
    context.lineWidth = 1;
    context.stroke();
    context.globalAlpha = 1;
  }

  function frame(deltaMs, time) {
    context.clearRect(0, 0, width, height);

    yaw = damp(yaw, targetYaw, 220, deltaMs);
    pitch = damp(pitch, targetPitch, 220, deltaMs);

    for (const path of paths) drawPath(path);

    // Collect every particle first, then sort once by depth and draw. Sorting per
    // stream would let a near particle from one stream be overdrawn by a far one
    // from the next, which reads as flicker rather than as depth.
    const sprites = [];
    for (const path of paths) {
      for (const particle of path.particles) {
        if (!reduced) {
          particle.t += particle.speed * (deltaMs / 16.67);
          if (particle.t > 1) particle.t -= 1;
        }
        const t = particle.t;
        const base = bezier(path.from, path.c1, path.c2, CORE, t);
        // Spread narrows to nothing as t approaches 1, so the streams visibly
        // converge instead of merely overlapping at the core.
        const narrowing = (1 - t) ** 1.5;
        const wobble = reduced ? 0 : Math.sin(time / 900 + particle.spread) * 8 * narrowing;
        const placed = view({
          x: base.x + particle.spread * narrowing + wobble,
          y: base.y + particle.spreadY * narrowing,
          z: base.z,
        });
        const screen = project(placed, { focal, cx, cy });
        if (!screen.visible) continue;
        sprites.push({
          x: screen.x,
          y: screen.y,
          z: placed.z,
          r: Math.max(0.35, particle.size * screen.scale),
          colour: path.colour,
          // Fade in from the far origin, then flare on arrival at the core.
          alpha: Math.min(1, 0.22 + t * 0.9) * Math.min(1, screen.scale * 1.15),
        });
      }
    }

    for (const sprite of depthSort(sprites)) {
      context.globalAlpha = sprite.alpha;
      context.fillStyle = sprite.colour;
      context.beginPath();
      context.arc(sprite.x, sprite.y, sprite.r, 0, Math.PI * 2);
      context.fill();
    }
    context.globalAlpha = 1;

    drawCore(time);
  }

  function drawCore(time) {
    const centre = project(view(CORE), { focal, cx, cy });
    if (!centre.visible) return;

    const pulse = reduced ? 0.5 : 0.5 + Math.sin(time / 1100) * 0.5;
    const radius = 8 + pulse * 3;

    // One radial gradient for the halo. This is the only place a gradient is built
    // per frame, and it is built once rather than per particle.
    if (config.glow) {
      const halo = context.createRadialGradient(
        centre.x, centre.y, 0,
        centre.x, centre.y, radius * 9
      );
      halo.addColorStop(0, withAlpha(palette.core, 0.34));
      halo.addColorStop(0.45, withAlpha(palette.payments, 0.14));
      halo.addColorStop(1, withAlpha(palette.payments, 0));
      context.fillStyle = halo;
      context.beginPath();
      context.arc(centre.x, centre.y, radius * 9, 0, Math.PI * 2);
      context.fill();
    }

    // The settlement ring: one closed books, drawn as a ring rather than a disc so it
    // reads as a total rather than as another particle.
    context.strokeStyle = withAlpha(palette.core, 0.75);
    context.lineWidth = 1.5;
    context.beginPath();
    context.arc(centre.x, centre.y, radius + 9 + pulse * 4, 0, Math.PI * 2);
    context.stroke();

    context.fillStyle = palette.core;
    context.beginPath();
    context.arc(centre.x, centre.y, radius * 0.55, 0, Math.PI * 2);
    context.fill();
  }

  /* --- loop --------------------------------------------------------------- */
  let raf = 0;
  let last = 0;
  let running = false;

  function tick(now) {
    // Clamp the delta. Returning to a backgrounded tab can hand you a delta of
    // several seconds, which would teleport every particle to a new position in one
    // frame - a visible jump on exactly the frame someone is looking at.
    const delta = Math.min(48, now - (last || now));
    last = now;
    frame(delta, now);
    if (running) raf = requestAnimationFrame(tick);
  }

  function start() {
    if (running || reduced) return;
    running = true;
    last = 0;
    raf = requestAnimationFrame(tick);
  }

  function stop() {
    running = false;
    cancelAnimationFrame(raf);
  }

  /* --- lifecycle ---------------------------------------------------------- */
  resize();
  // Compose one frame immediately, so the hero is never an empty box - and so that
  // under reduced motion there is still a complete, considered picture.
  frame(16, 0);

  const onResize = () => {
    resize();
    if (!running) frame(16, performance.now());
  };
  window.addEventListener("resize", onResize, { passive: true });

  const onVisibility = () => (document.hidden ? stop() : start());
  document.addEventListener("visibilitychange", onVisibility);

  let observer = null;
  if ("IntersectionObserver" in window) {
    observer = new IntersectionObserver(
      ([entry]) => (entry.isIntersecting ? start() : stop()),
      { threshold: 0.05 }
    );
    observer.observe(canvas);
  } else {
    start();
  }

  // Pointer parallax only where there is a pointer. On touch this would need a drag,
  // and a hero that swallows drags is a hero that breaks scrolling.
  const fine = window.matchMedia?.("(pointer: fine)").matches ?? true;
  if (fine && !reduced) {
    window.addEventListener("pointermove", onPointerMove, { passive: true });
    canvas.addEventListener("pointerleave", onPointerLeave);
  }

  return function destroy() {
    stop();
    observer?.disconnect();
    window.removeEventListener("resize", onResize);
    document.removeEventListener("visibilitychange", onVisibility);
    window.removeEventListener("pointermove", onPointerMove);
    canvas.removeEventListener("pointerleave", onPointerLeave);
  };
}

/** Apply an alpha to a #rrggbb token value. */
function withAlpha(hex, alpha) {
  const match = /^#([0-9a-f]{6})$/i.exec(hex.trim());
  if (!match) return hex;
  const n = parseInt(match[1], 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}
