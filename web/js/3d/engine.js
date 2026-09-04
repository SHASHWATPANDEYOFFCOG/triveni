/* A small 3D engine: rotation, perspective projection, depth sorting.
 *
 * ---------------------------------------------------------------------------
 * WHY NOT THREE.JS
 * ---------------------------------------------------------------------------
 * Three.js is the right answer for meshes, materials and lighting. This scene is a
 * few thousand points, three curves and some lines - a particle field, not a model.
 * For that, Three brings ~600KB and a build step to a problem that is 120 lines of
 * arithmetic.
 *
 * The deciding constraint is the project's, not a preference: `make demo` has to run
 * on a clean clone with no network and no `npm install`. Vendoring a minified bundle
 * into the repository to preserve that would trade one honest sentence in the README
 * for a large opaque blob nobody reviews.
 *
 * The real win is elsewhere, though. This file is pure functions over plain numbers,
 * so it can be unit-tested in Node with no DOM, no WebGL and no headless browser -
 * see `tests/test_dashboard.py`. That matters more than usual here, because a browser
 * is exactly what this project does not have in CI, and a `[hidden]` bug that took
 * the whole dashboard down already shipped past 74 tests that could not see a
 * rendered page. Maths that can be checked is maths that will not silently invert.
 *
 * ---------------------------------------------------------------------------
 * CONVENTIONS
 * ---------------------------------------------------------------------------
 *   +x right, +y down (screen convention, so no flip at draw time), +z away.
 *   The camera sits at the origin looking down +z. A point needs z > -focal to be
 *   in front of it.
 */

/** Rotate about the Y axis (yaw). Mouse X drives this. */
export function rotateY({ x, y, z }, angle) {
  const c = Math.cos(angle);
  const s = Math.sin(angle);
  return { x: x * c + z * s, y, z: z * c - x * s };
}

/** Rotate about the X axis (pitch). Mouse Y drives this. */
export function rotateX({ x, y, z }, angle) {
  const c = Math.cos(angle);
  const s = Math.sin(angle);
  return { x, y: y * c - z * s, z: y * s + z * c };
}

/**
 * Pinhole projection.
 *
 * `scale = focal / (focal + z)` is the whole of it: at z = 0 the scale is 1 and the
 * point lands at its unprojected offset from the centre; as z grows the scale falls
 * toward 0 and the point converges on the vanishing point. Behind the camera the
 * denominator flips sign, which would mirror the point through the origin and draw
 * it, wrongly, on screen - so that case is reported as `visible: false` rather than
 * clamped. Clamping is how a particle field ends up with a smear of points pinned to
 * the centre.
 */
export function project(point, { focal = 900, cx = 0, cy = 0 } = {}) {
  const denominator = focal + point.z;
  if (denominator <= 1e-6) {
    return { x: 0, y: 0, scale: 0, visible: false };
  }
  const scale = focal / denominator;
  return {
    x: cx + point.x * scale,
    y: cy + point.y * scale,
    scale,
    visible: true,
  };
}

/**
 * Painter's algorithm: far things first, so near things overdraw them.
 *
 * Returns a new array. Sorting the caller's array in place would reorder the
 * simulation's own particle list every frame, which quietly destroys any per-particle
 * state that is indexed by position.
 */
export function depthSort(points) {
  return [...points].sort((a, b) => b.z - a.z);
}

/**
 * Frame-rate-independent exponential smoothing.
 *
 * `lerp(current, target, 0.1)` per frame is the usual shortcut, and it means the
 * animation runs at a different speed on a 144Hz screen than on a 60Hz one. This
 * takes the elapsed time and a half-life, so the motion is identical everywhere and
 * a dropped frame catches up instead of stuttering.
 */
export function damp(current, target, halfLifeMs, deltaMs) {
  if (halfLifeMs <= 0) return target;
  const ratio = 1 - Math.pow(2, -deltaMs / halfLifeMs);
  return current + (target - current) * ratio;
}

/** A cubic Bezier point at t. Used for the three converging streams. */
export function bezier(p0, p1, p2, p3, t) {
  const u = 1 - t;
  const a = u * u * u;
  const b = 3 * u * u * t;
  const c = 3 * u * t * t;
  const d = t * t * t;
  return {
    x: a * p0.x + b * p1.x + c * p2.x + d * p3.x,
    y: a * p0.y + b * p1.y + c * p2.y + d * p3.y,
    z: a * p0.z + b * p1.z + c * p2.z + d * p3.z,
  };
}

/**
 * A deterministic PRNG (mulberry32).
 *
 * `Math.random()` would give a different particle layout on every reload, which makes
 * a visual regression impossible to see and a screenshot impossible to reproduce. The
 * scene is seeded, so it is the same composition every time - and, like everything
 * else in this project, reproducible.
 */
export function rng(seed = 20260101) {
  let a = seed >>> 0;
  return function next() {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
