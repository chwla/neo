import assert from "node:assert/strict";
import { test } from "node:test";
import { effectById } from "../src/backgrounds/effects.js";
import { INTENSITIES } from "../src/backgrounds/index.js";
import { rgba } from "../src/backgrounds/palette.js";

const palette = { accent: [57, 255, 20], ink: [255, 255, 255], isLight: false, glowMode: "lighter", rgba };

function context() {
  const counts = {};
  const paths = [];
  const stack = [];
  let path = [];
  const ctx = {
    globalAlpha: 1, globalCompositeOperation: "source-over", lineCap: "butt",
    createLinearGradient: gradient, createRadialGradient: gradient,
    beginPath() { path = []; },
    moveTo(...values) { path.push({ op: "move", values }); },
    lineTo(...values) { path.push({ op: "line", values }); },
    bezierCurveTo(...values) { path.push({ op: "cubic", values }); },
    quadraticCurveTo(...values) { path.push({ op: "quadratic", values }); },
    arc() {}, closePath() {}, fill() {}, translate() {}, rotate() {}, scale() {},
    stroke() { paths.push(path); counts.strokes = (counts.strokes || 0) + 1; },
    save() { stack.push([this.globalAlpha, this.globalCompositeOperation, this.lineCap]); },
    restore() { [this.globalAlpha, this.globalCompositeOperation, this.lineCap] = stack.pop(); },
  };
  function gradient() { counts.gradients = (counts.gradients || 0) + 1; return { addColorStop() {} }; }
  return { ctx, counts, paths };
}

function seedRandom(seed = 7) {
  const original = Math.random;
  Math.random = () => { seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; return seed / 2 ** 32; };
  return () => { Math.random = original; };
}

// An independent continuous-curve oracle. The rendering uses cached bases and
// Hermite controls; this evaluates the design equations directly at arbitrary
// positions, so a wrong tangent or segment endpoint cannot pass via snapshots.
function ribbonY(r, guide, x, width, height, time) {
  const side = r % 2 === 0 ? 1 : -1;
  const centre = 0.3 + r * 0.19;
  const home = centre + (guide ? 0.045 : -0.045);
  const swing = guide ? 0.085 + (r % 3) * 0.022 : 0.1 + (r % 2) * 0.03;
  const cycles = guide ? [0.8 - r * 0.1, 2.1 + r * 0.3, 3.7 + r * 0.35] : [0.65 + r * 0.15, 1.7 + r * 0.25, 3.1 + r * 0.4];
  const speed = guide ? [-0.29 * side, 0.37, -0.19 * side] : [0.34 * side, -0.23, 0.41 * side];
  const phases = guide ? [r * 2.1 + 1.1, r * 1.6 + 2.4, r * 3.1] : [r * 1.3, r * 2.7 + 0.6, r * 0.9 + 1.8];
  const weights = [1, 0.44, 0.19];
  const offset = cycles.reduce((sum, cycle, i) => sum + weights[i] * Math.sin(x / width * 2 * Math.PI * cycle + time * speed[i] + phases[i]), 0);
  return height * (home + 0.06 * Math.sin(time * (0.061 + r * 0.013) + r * 2.2) + swing * offset);
}

for (const intensity of INTENSITIES) {
  test(`gradient: ${intensity.id} keeps the continuous ribbon within 0.15px with fewer path commands`, () => {
    const lines = Math.max(24, Math.round(48 * intensity.density));
    const ribbons = Math.max(2, Math.min(3, Math.round(3 * intensity.density)));
    const effect = effectById("gradient").create({ width: 800, height: 600, palette, intensity });
    for (const [width, height] of [[320, 200], [800, 600], [1512, 982], [3024, 1964]]) {
      effect.resize(width, height);
      for (const time of [0, 1.5, 29, 113]) {
        const { ctx, paths } = context();
        effect.frame(ctx, 1 / 60, time);
        assert.equal(paths.length, lines * ribbons, "additive hairlines must still composite independently");
        for (let r = 0; r < ribbons; r += 1) {
          for (const i of [0, Math.floor(lines / 2), lines - 1]) {
            const raw = i / (lines - 1);
            const across = (raw + raw * raw * (3 - 2 * raw)) / 2;
            const commands = paths[r * lines + i];
            let [x0, y0] = commands[0].values;
            assert.ok(commands.length < 50, "curve submission regressed toward a dense polyline");
            for (const command of commands.slice(1)) {
              assert.equal(command.op, "cubic");
              const [x1, y1, x2, y2, x3, y3] = command.values;
              for (const u of [0.125, 0.25, 0.5, 0.75, 0.875, 1]) {
                const v = 1 - u;
                const x = v ** 3 * x0 + 3 * v * v * u * x1 + 3 * v * u * u * x2 + u ** 3 * x3;
                const y = v ** 3 * y0 + 3 * v * v * u * y1 + 3 * v * u * u * y2 + u ** 3 * y3;
                const a = ribbonY(r, 0, x, width, height, time);
                const b = ribbonY(r, 1, x, width, height, time);
                assert.ok(Math.abs(y - (a + (b - a) * across)) <= 0.150001,
                  `ribbon ${r}, time ${time}, x ${x}: curve differs by ${Math.abs(y - (a + (b - a) * across))}px`);
              }
              [x0, y0] = [x3, y3];
            }
            assert.equal(x0, width, "the curve must reach the far edge after resize");
          }
        }
        assert.equal(ctx.globalAlpha, 1);
        assert.equal(ctx.globalCompositeOperation, "source-over");
      }
    }
  });
}

test("stars reuse two meteor gradients across motion and refresh them on retint", () => {
  const restore = seedRandom();
  try {
    const effect = effectById("stars").create({ width: 1512, height: 982, palette, intensity: INTENSITIES[2] });
    const { ctx, counts } = context();
    for (let f = 0; f < 240; f += 1) effect.frame(ctx, 1 / 60, f / 60);
    assert.ok(counts.strokes > 100, "the run must contain active meteors");
    assert.equal(counts.gradients, 2, "motion must not allocate gradients");
    effect.retint({ ...palette, accent: [10, 80, 30], isLight: true, glowMode: "source-over" });
    effect.frame(ctx, 1 / 60, 4);
    assert.equal(counts.gradients, 4, "both caches must reflect the new theme");
    assert.equal(ctx.globalAlpha, 1);
    assert.equal(ctx.globalCompositeOperation, "source-over");
    assert.equal(ctx.lineCap, "butt");
  } finally { restore(); }
});

test("rain simulates offscreen drops without submitting invisible strokes", () => {
  const restore = seedRandom();
  try {
    const effect = effectById("rain").create({ width: 1512, height: 982, palette, intensity: INTENSITIES[2] });
    const { ctx, counts } = context();
    for (let f = 0; f < 240; f += 1) effect.frame(ctx, 1 / 60, f / 60);
    assert.ok(counts.strokes > 15_000, "visible rainfall must remain populated");
    assert.ok(counts.strokes < 26_000, "invisible drops should avoid path submission");
    assert.equal(counts.gradients, 1);
    assert.equal(ctx.globalAlpha, 1);
  } finally { restore(); }
});
