/* Unit tests for the 3D maths, run under plain Node - no DOM, no WebGL, no browser.
 *
 * This exists because the project has no headless browser, and a rendering bug that
 * is only visible on screen is a bug nothing in CI can see. Projection and rotation
 * are the parts that can be wrong in a way that still *draws something* - an inverted
 * axis, a sign error behind the camera, a scale that grows with distance - and every
 * one of those is checkable as arithmetic.
 *
 * Run: node web/js/3d/engine.test.mjs   (also run by tests/test_dashboard.py)
 */

import assert from "node:assert/strict";
import { bezier, damp, depthSort, project, rng, rotateX, rotateY } from "./engine.js";

const near = (a, b, tolerance = 1e-9) =>
  assert.ok(Math.abs(a - b) < tolerance, `${a} !== ${b}`);

let passed = 0;
const test = (name, fn) => {
  fn();
  passed += 1;
  console.log(`  ok  ${name}`);
};

/* --- rotation ------------------------------------------------------------- */
test("a full turn is the identity", () => {
  const p = { x: 3, y: -5, z: 7 };
  const spun = rotateY(p, Math.PI * 2);
  near(spun.x, p.x, 1e-12);
  near(spun.z, p.z, 1e-12);
});

test("rotation preserves length", () => {
  const p = { x: 3, y: -5, z: 7 };
  const length = (q) => Math.hypot(q.x, q.y, q.z);
  near(length(rotateY(p, 0.7)), length(p), 1e-12);
  near(length(rotateX(p, -1.3)), length(p), 1e-12);
});

test("a quarter turn about Y sends +x to -z", () => {
  // With +x right and +z away, yawing by +90 degrees must swing the right-hand axis
  // away from the camera. Getting this backwards is the classic inverted-drag bug:
  // the scene rotates the opposite way to the mouse and feels broken without ever
  // looking broken in a still screenshot.
  const spun = rotateY({ x: 1, y: 0, z: 0 }, Math.PI / 2);
  near(spun.x, 0, 1e-12);
  near(spun.z, -1, 1e-12);
});

test("Y rotation leaves y alone and X rotation leaves x alone", () => {
  near(rotateY({ x: 2, y: 9, z: 4 }, 1.1).y, 9);
  near(rotateX({ x: 2, y: 9, z: 4 }, 1.1).x, 2);
});

/* --- projection ----------------------------------------------------------- */
test("at z = 0 a point projects at unit scale", () => {
  const out = project({ x: 10, y: -4, z: 0 }, { focal: 900, cx: 100, cy: 50 });
  near(out.scale, 1);
  near(out.x, 110);
  near(out.y, 46);
});

test("further away is smaller, and monotonically so", () => {
  let previous = Infinity;
  for (const z of [0, 100, 300, 900, 2000]) {
    const { scale } = project({ x: 1, y: 1, z }, { focal: 900 });
    assert.ok(scale < previous, `scale must fall with depth (z=${z})`);
    previous = scale;
  }
});

test("points behind the camera are culled, not mirrored", () => {
  // focal + z <= 0. Without the guard the scale goes negative and the point is drawn
  // mirrored through the vanishing point - visible, plausible, and completely wrong.
  assert.equal(project({ x: 5, y: 5, z: -900 }, { focal: 900 }).visible, false);
  assert.equal(project({ x: 5, y: 5, z: -2000 }, { focal: 900 }).visible, false);
  assert.equal(project({ x: 5, y: 5, z: -899 }, { focal: 900 }).visible, true);
});

test("the projection converges on the vanishing point", () => {
  const far = project({ x: 500, y: 500, z: 1e7 }, { focal: 900, cx: 0, cy: 0 });
  assert.ok(Math.abs(far.x) < 0.1 && Math.abs(far.y) < 0.1);
});

/* --- depth sort ----------------------------------------------------------- */
test("depthSort draws far points first and does not mutate its input", () => {
  const points = [{ z: 1 }, { z: 9 }, { z: 5 }];
  const sorted = depthSort(points);
  assert.deepEqual(
    sorted.map((p) => p.z),
    [9, 5, 1]
  );
  assert.deepEqual(
    points.map((p) => p.z),
    [1, 9, 5],
    "sorting in place would reorder the simulation's own particle list every frame"
  );
});

/* --- damping -------------------------------------------------------------- */
test("damp reaches the halfway point after exactly one half-life", () => {
  near(damp(0, 100, 100, 100), 50, 1e-9);
});

test("damp is frame-rate independent", () => {
  // One 32ms step must land in the same place as two 16ms steps, or the scene drifts
  // at a different speed on a 144Hz monitor than on a 60Hz one.
  const single = damp(0, 100, 120, 32);
  const double = damp(damp(0, 100, 120, 16), 100, 120, 16);
  near(single, double, 1e-9);
});

test("damp with a zero half-life snaps", () => assert.equal(damp(3, 8, 0, 16), 8));

/* --- bezier --------------------------------------------------------------- */
test("a bezier starts at p0 and ends at p3", () => {
  const p0 = { x: 0, y: 0, z: 0 };
  const p1 = { x: 1, y: 2, z: 3 };
  const p2 = { x: 4, y: 5, z: 6 };
  const p3 = { x: 9, y: 8, z: 7 };
  assert.deepEqual(bezier(p0, p1, p2, p3, 0), p0);
  const end = bezier(p0, p1, p2, p3, 1);
  near(end.x, 9);
  near(end.y, 8);
  near(end.z, 7);
});

/* --- rng ------------------------------------------------------------------ */
test("the rng is deterministic and stays in [0, 1)", () => {
  const a = rng(42);
  const b = rng(42);
  for (let i = 0; i < 500; i += 1) {
    const value = a();
    assert.equal(value, b(), "same seed must give the same sequence");
    assert.ok(value >= 0 && value < 1);
  }
});

test("different seeds give different sequences", () => {
  assert.notEqual(rng(1)(), rng(2)());
});

console.log(`\n${passed} assertions passed`);
