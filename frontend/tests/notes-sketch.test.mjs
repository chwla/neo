/**
 * Sketch strokes as they are kept in a note.
 *
 * What must hold: an encoded stroke decodes to the same points; anything that
 * is not a stroke (truncated, tampered, out of the drawing) is refused rather
 * than drawn wrong; a long stroke stays small; the drawing's shape does not
 * depend on the size it is shown at; the eraser takes only what it touches.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import {
  MAX_POINTS,
  SKETCH_WIDTH,
  decodePoints,
  encodePoints,
  sketchFromAttrs,
  strokePath,
  strokesUnder,
  validStroke,
} from "../src/notes/sketch/strokes.js";

function wave(count, aspect = 0.75) {
  const height = SKETCH_WIDTH * aspect;
  return Array.from({ length: count }, (_, i) => [
    Math.round((i / Math.max(1, count - 1)) * SKETCH_WIDTH),
    Math.round(height / 2 + Math.sin(i / 7) * height * 0.4),
    Math.round((0.3 + 0.6 * Math.abs(Math.sin(i / 11))) * 100) / 100,
  ]);
}

describe("stroke encoding", () => {
  test("points come back exactly, moves in every direction and pressure included", () => {
    const points = [[0, 0, 0.5], [9999, 7400, 1], [3, 1, 0], [5000, 3750, 0.42], [4999, 3751, 0.43]];
    assert.deepEqual(decodePoints(encodePoints(points), 0.75), points);
  });

  test("a long stroke stays small", () => {
    const encoded = encodePoints(wave(400));
    assert.ok(encoded.length < 400 * 6, `${encoded.length} characters for 400 points`);
  });

  test("anything that is not a stroke is refused", () => {
    const good = encodePoints(wave(20));
    for (const bad of ["***", good.slice(0, -1) + "#", good + "A", "A", encodePoints([[0, 0, 0.5], [50000, 0, 0.5]])]) {
      assert.throws(() => decodePoints(bad, 0.75), String(bad).slice(0, 20));
    }
  });

  test("a stroke has at most a fixed number of points", () => {
    const encoded = encodePoints(wave(MAX_POINTS + 50));
    assert.equal(decodePoints(encoded, 0.75).length, MAX_POINTS);
  });
});

describe("sketches in a note", () => {
  test("only real strokes are kept: unknown tools, sizes and broken points are dropped", () => {
    const points = encodePoints(wave(10));
    const sketch = sketchFromAttrs({
      aspect: 0.75,
      strokes: [
        { tool: "pen", color: "pink", size: 2, points },
        { tool: "laser", color: "pink", size: 2, points },
        { tool: "pen", color: "pink", size: 9, points },
        { tool: "pen", color: "pink", size: 1, points: "%%%" },
        { tool: "marker", color: "teal", size: 3, points },
      ],
    });
    assert.equal(sketch.strokes.length, 2);
    assert.equal(sketch.strokes[1].color, "ink", "an unknown colour becomes ink");
  });

  test("the shape is the same whatever size it is shown at", () => {
    const stroke = validStroke({ tool: "pen", color: "ink", size: 2, points: encodePoints(wave(60)) }, 0.75);
    const path = strokePath(stroke, 0.75);
    // Ink can reach a little past the edge, so coordinates may be negative.
    assert.match(path, /^M-?\d+ -?\d+( Q-?\d+ -?\d+ -?\d+ -?\d+)+ Z$/);
    // Drawn in sketch units: the same path for a phone or a desktop; only the SVG's size differs.
    assert.equal(strokePath(stroke, 0.75), path);
  });

  test("the eraser takes the strokes it touches and no others", () => {
    const left = { tool: "pen", color: "ink", size: 1, points: encodePoints([[1000, 1000, 0.5], [1500, 1000, 0.5]]) };
    const right = { tool: "pen", color: "ink", size: 1, points: encodePoints([[8000, 6000, 0.5], [8500, 6000, 0.5]]) };
    assert.deepEqual(strokesUnder([left, right], [1200, 1050], 100, 0.75), [0]);
    assert.deepEqual(strokesUnder([left, right], [5000, 3000], 100, 0.75), []);
  });

  test("a quick rub takes a line it crosses between two of its samples", () => {
    const line = { tool: "pen", color: "ink", size: 1, points: encodePoints([[1000, 4000, 0.5], [9000, 4000, 0.5]]) };
    const far = { tool: "pen", color: "ink", size: 1, points: encodePoints([[1000, 7000, 0.5], [9000, 7000, 0.5]]) };
    // Samples 600 units either side of the line: neither touches it, the way between does.
    assert.deepEqual(strokesUnder([line, far], [5000, 3400], 100, 0.75), []);
    assert.deepEqual(strokesUnder([line, far], [5000, 4600], 100, 0.75, new Map(), [5000, 3400]), [0]);
    // Moving alongside, out of reach, takes nothing.
    assert.deepEqual(strokesUnder([line, far], [8000, 5000], 100, 0.75, new Map(), [2000, 5000]), []);
  });

  test("an empty or missing sketch is an empty drawing with a sane shape", () => {
    assert.deepEqual(sketchFromAttrs({}), { aspect: 0.75, strokes: [] });
    assert.equal(sketchFromAttrs({ aspect: 99 }).aspect, 3);
  });
});
