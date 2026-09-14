import assert from "node:assert/strict";
import { test } from "node:test";
import { backgroundDpr, createRenderBudget, MAX_BACKGROUND_PIXELS } from "../src/backgrounds/renderBudget.js";

test("backing pixels are bounded even on a large Retina external display", () => {
  for (const [width, height, deviceDpr] of [[800, 600, 3], [3840, 2160, 2], [7680, 4320, 2]]) {
    const dpr = backgroundDpr(width, height, deviceDpr);
    assert.ok(width * height * dpr * dpr <= MAX_BACKGROUND_PIXELS + 1);
    assert.ok(dpr <= deviceDpr && dpr <= 1.5);
    assert.ok(backgroundDpr(width, height, deviceDpr, 2) < dpr);
  }
  assert.equal(backgroundDpr(800, 600, 1), 1);
});

function drive(budget, { from = 0, seconds, cost = 0.5, interval = 1000 / 60 }) {
  const end = from + seconds * 1000;
  for (let now = from; now < end; now += interval) {
    budget.refresh(interval);
    budget.record(now, cost);
  }
  return end;
}

test("sustained work lowers resolution first, then cadence, and recovers with headroom", () => {
  const budget = createRenderBudget();
  let now = drive(budget, { seconds: 2.3, cost: 10 });
  assert.equal(budget.level, 1);
  assert.equal(budget.fps, 60);
  now = drive(budget, { from: now, seconds: 5, cost: 10 });
  assert.equal(budget.level, 3);
  assert.equal(budget.fps, 30);
  now = drive(budget, { from: now, seconds: 5 });
  assert.equal(budget.level, 3, "a short idle period must not immediately oscillate quality");
  drive(budget, { from: now, seconds: 70 });
  assert.equal(budget.level, 0);
  assert.equal(budget.fps, 60);
});

test("isolated stalls and naturally slow displays do not lower quality", () => {
  for (const interval of [1000 / 30, 1000 / 60, 1000 / 120]) {
    const budget = createRenderBudget();
    drive(budget, { seconds: 6, interval });
    budget.refresh(3000);
    budget.record(6000, 100);
    drive(budget, { from: 6000, seconds: 6, interval });
    assert.equal(budget.level, 0);
  }
});

test("missed display deadlines trigger adaptation even when JS submission is cheap", () => {
  const budget = createRenderBudget();
  drive(budget, { seconds: 1 });
  drive(budget, { from: 1000, seconds: 4, interval: 1000 / 30 });
  assert.ok(budget.level > 0);
});

test("visibility resets ignore old timing pressure without resetting the chosen quality", () => {
  const budget = createRenderBudget();
  drive(budget, { seconds: 2.3, cost: 10 });
  assert.equal(budget.level, 1);
  budget.reset();
  drive(budget, { from: 50_000, seconds: 1, interval: 1000 / 30 });
  assert.equal(budget.level, 1);
});
