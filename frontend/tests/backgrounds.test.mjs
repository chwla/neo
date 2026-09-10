/**
 * The background catalogue, its picker, and the engine's lifecycle.
 *
 * The catalogue tests are the theme suite's, for the same failure modes: an id
 * with no module renders an empty canvas and looks like nothing happened, and
 * `applyBackground` writing an unknown id would leave the attribute naming a
 * background that cannot be drawn.
 *
 * The lifecycle tests are the ones worth having. Neo renders under StrictMode,
 * so every engine is built, torn down and built again in development, and the
 * picker rebuilds one on each choice. None of that shows up as an error if
 * teardown is incomplete -- it shows up as a second loop drawing over the first
 * at twice the frame cost, which is invisible until it is four loops.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import BackgroundSettings from "../src/BackgroundSettings.jsx";
import ChatBackground from "../src/ChatBackground.jsx";
import {
  BACKGROUNDS,
  DEFAULT_BACKGROUND_ID,
  DEFAULT_INTENSITY_ID,
  INTENSITIES,
  applyBackground,
  backgroundById,
  intensityById,
  isBackgroundId,
} from "../src/backgrounds/index.js";
import { EFFECT_IDS, effectById } from "../src/backgrounds/effects.js";
import { createEngine } from "../src/backgrounds/engine.js";
import { luminance, parseHex, rgba } from "../src/backgrounds/palette.js";

const CSS = readFileSync(new URL("../src/index.css", import.meta.url), "utf8");

/**
 * Every declaration block in the stylesheet, as selector and body.
 *
 * The pattern matches innermost braces only, so a rule inside `@media` is found
 * by its own selector rather than by the query wrapping it, and an at-rule
 * prelude never comes back as a rule of its own. Comments go first: this file's
 * prose names the very properties the tests below look for, and a comment sits
 * in the selector text of the rule it introduces.
 *
 * Trimming to the last `;` drops the brace-less statements a selector's text
 * would otherwise carry -- the sheet opens with three `@tailwind` lines, which
 * arrive glued to the front of `:root`.
 */
const RULES = [...CSS.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/([^{}]*)\{([^{}]*)\}/g)]
  .map(([, selector, body]) => ({ selector: selector.split(";").pop().trim(), body }));

function rulesMentioning(needle) {
  return RULES.filter((rule) => rule.selector.includes(needle));
}

function ruleFor(selector) {
  const found = RULES.filter((rule) => rule.selector === selector);
  assert.equal(found.length, 1, `${selector} is not a rule of its own`);
  return found[0].body;
}

function render(background, intensity = DEFAULT_INTENSITY_ID) {
  return renderToStaticMarkup(
    createElement(BackgroundSettings, {
      background,
      intensity,
      onBackgroundChange() {},
      onIntensityChange() {},
      onClose() {},
    }),
  );
}

function checkedCards(html, label) {
  const group = html.split(`aria-label="${label}"`)[1].split("</div>")[0];
  const cards = group.split("<button").filter((chunk) => chunk.includes('role="radio"'));
  return {
    cards,
    checked: cards.filter((chunk) => chunk.includes('aria-checked="true"')),
  };
}

describe("the background catalogue", () => {
  test("ships none, jellyfish, stars, rain and waves", () => {
    assert.deepEqual(
      BACKGROUNDS.map((entry) => entry.id),
      ["none", "jellyfish", "stars", "rain", "waves"],
    );
  });

  test("every background but none has a module that can draw it", () => {
    for (const entry of BACKGROUNDS) {
      if (entry.id === DEFAULT_BACKGROUND_ID) continue;
      assert.ok(effectById(entry.id), `${entry.id} has a card but nothing draws it`);
    }
  });

  test("no module exists that the catalogue does not offer", () => {
    // The other direction: a module nobody can choose is dead weight that still
    // gets bundled.
    for (const id of EFFECT_IDS) {
      assert.ok(isBackgroundId(id), `${id} draws but has no card`);
    }
  });

  test("none is the default and has no module of its own", () => {
    assert.equal(DEFAULT_BACKGROUND_ID, "none");
    // Not an effect that draws nothing -- the absence of an effect, so that
    // choosing it mounts no canvas and starts no loop.
    assert.equal(effectById("none"), undefined);
  });

  test("ids are unique, lowercase, and every card has its copy", () => {
    assert.equal(new Set(BACKGROUNDS.map((entry) => entry.id)).size, BACKGROUNDS.length);
    for (const entry of BACKGROUNDS) {
      // The test holding this catalogue and app/services/appearance.py together
      // reads these ids out of the file with a regular expression.
      assert.match(entry.id, /^[a-z]+$/, `${entry.id} is not a bare lowercase id`);
      assert.ok(entry.name && entry.description, `${entry.id} is missing its copy`);
    }
  });

  test("a stale id looks up as undefined rather than throwing", () => {
    assert.equal(backgroundById("fireflies"), undefined);
    assert.equal(isBackgroundId("fireflies"), false);
    assert.ok(backgroundById(DEFAULT_BACKGROUND_ID));
  });

  test("intensities are the three the effects scale by", () => {
    assert.deepEqual(INTENSITIES.map((entry) => entry.id), ["subtle", "medium", "vivid"]);
    assert.equal(DEFAULT_INTENSITY_ID, "medium");
    // Medium is the identity, so an effect's own tuned numbers are what it says.
    assert.equal(intensityById("medium").alpha, 1);
    assert.equal(intensityById("medium").density, 1);
  });

  test("an unknown intensity still yields usable multipliers", () => {
    // Never undefined: the effects multiply by these, and NaN paints nothing.
    assert.equal(intensityById("blinding").id, DEFAULT_INTENSITY_ID);
  });
});

describe("applying a background", () => {
  test("sets the attribute the stylesheet selects on", () => {
    const root = { dataset: {} };
    applyBackground("jellyfish", root);
    assert.equal(root.dataset.chatBg, "jellyfish");
  });

  test("none removes the attribute rather than naming itself", () => {
    const root = { dataset: { chatBg: "rain" } };
    applyBackground(DEFAULT_BACKGROUND_ID, root);
    assert.equal(root.dataset.chatBg, undefined);
  });

  test("an unknown id falls back to none instead of naming a missing module", () => {
    const root = { dataset: { chatBg: "waves" } };
    applyBackground("fireflies", root);
    assert.equal(root.dataset.chatBg, undefined, "a card that cannot draw is worse than none");
  });
});

describe("the palette a canvas can read", () => {
  test("parses both hex forms the stylesheet uses", () => {
    // The default theme's ink is written #fff.
    assert.deepEqual(parseHex("#fff"), [255, 255, 255]);
    assert.deepEqual(parseHex("#39ff14"), [57, 255, 20]);
    assert.deepEqual(parseHex("  #0A0A0A "), [10, 10, 10]);
  });

  test("anything that is not a hex reads as null so the caller can fall back", () => {
    // A derived token reads back unresolved, and drawing in NaN paints nothing
    // and says nothing about why.
    assert.equal(parseHex("color-mix(in srgb, var(--neo-accent) 20%, transparent)"), null);
    assert.equal(parseHex(""), null);
    assert.equal(parseHex(undefined), null);
  });

  test("tells Paper apart from every dark theme", () => {
    assert.ok(luminance([250, 248, 244]) > 0.5, "paper is a light ground");
    assert.ok(luminance([10, 10, 10]) < 0.5, "default is not");
    assert.ok(luminance([22, 22, 28]) < 0.5, "nor is indigo, the lightest dark one");
  });

  test("alpha is clamped, because vivid multiplies past one", () => {
    assert.equal(rgba([1, 2, 3], 1.6), "rgba(1, 2, 3, 1)");
    assert.equal(rgba([1, 2, 3], -0.2), "rgba(1, 2, 3, 0)");
  });
});

describe("the picker", () => {
  test("offers every background", () => {
    const html = render(DEFAULT_BACKGROUND_ID);
    for (const entry of BACKGROUNDS) {
      assert.ok(html.includes(entry.name), `${entry.name} is not offered`);
    }
  });

  test("marks the current background as the chosen radio", () => {
    const { cards, checked } = checkedCards(render("jellyfish"), "Background");

    assert.equal(cards.length, BACKGROUNDS.length);
    assert.equal(checked.length, 1, "exactly one background is current");
    assert.ok(checked[0].includes("Jellyfish"));
  });

  test("is a radiogroup, so arrow keys move between backgrounds", () => {
    assert.ok(render(DEFAULT_BACKGROUND_ID).includes('role="radiogroup"'));
  });

  test("offers every intensity and marks exactly one", () => {
    const { cards, checked } = checkedCards(render("rain", "vivid"), "Background intensity");

    assert.equal(cards.length, INTENSITIES.length);
    assert.equal(checked.length, 1, "exactly one intensity is current");
    assert.ok(checked[0].includes("Vivid"));
  });

  test("says the layer does not take clicks and that it honours reduced motion", () => {
    // The two questions someone asks before turning motion on behind their work.
    const html = render(DEFAULT_BACKGROUND_ID);
    assert.ok(html.includes("never takes a click"));
    assert.ok(html.includes("reduced motion"));
  });
});

/* --------------------------------------------------------------------------
   The engine, against a recorded fake document.
   -------------------------------------------------------------------------- */

function installDom({ reduceMotion = false, hidden = false } = {}) {
  const state = {
    frames: new Map(),
    nextHandle: 1,
    now: 0,
    draws: 0,
    resizeObservers: 0,
    mutationObservers: 0,
    visibilityListeners: 0,
    motionListeners: 0,
    hidden,
  };

  const gradient = { addColorStop() {} };
  const context = new Proxy(
    {},
    {
      get(target, prop) {
        if (typeof prop === "symbol") return undefined;
        if (prop === "createLinearGradient" || prop === "createRadialGradient") {
          return () => gradient;
        }
        if (prop in target) return target[prop];
        return () => {
          state.draws += 1;
        };
      },
      set(target, prop, value) {
        target[prop] = value;
        return true;
      },
    },
  );

  const canvas = { style: {}, width: 0, height: 0, getContext: () => context };
  const host = { getBoundingClientRect: () => ({ width: 800, height: 600 }) };

  const saved = {};
  const define = (name, value) => {
    saved[name] = globalThis[name];
    globalThis[name] = value;
  };

  define("ResizeObserver", class {
    constructor(callback) {
      this.callback = callback;
      state.resizeObservers += 1;
    }
    observe() {}
    disconnect() {
      state.resizeObservers -= 1;
    }
  });

  define("MutationObserver", class {
    constructor(callback) {
      this.callback = callback;
      state.mutationObservers += 1;
    }
    observe() {}
    disconnect() {
      state.mutationObservers -= 1;
    }
  });

  define("requestAnimationFrame", (callback) => {
    const handle = state.nextHandle++;
    state.frames.set(handle, callback);
    return handle;
  });
  define("cancelAnimationFrame", (handle) => {
    state.frames.delete(handle);
  });

  define("getComputedStyle", () => ({
    getPropertyValue(name) {
      if (name === "--neo-accent") return "#39ff14";
      if (name === "--neo-bg") return "#0a0a0a";
      return "#fff";
    },
  }));

  define("matchMedia", () => ({
    matches: reduceMotion,
    addEventListener() {
      state.motionListeners += 1;
    },
    removeEventListener() {
      state.motionListeners -= 1;
    },
  }));

  define("window", { devicePixelRatio: 3 });

  define("document", {
    get hidden() {
      return state.hidden;
    },
    documentElement: { dataset: {} },
    addEventListener() {
      state.visibilityListeners += 1;
    },
    removeEventListener() {
      state.visibilityListeners -= 1;
    },
  });

  state.canvas = canvas;
  state.host = host;
  //: Runs one frame's worth of scheduled callbacks. A running engine reschedules
  //: itself, so the count that comes back is how many loops are alive.
  state.flush = () => {
    const pending = [...state.frames.entries()];
    state.frames.clear();
    state.now += 16;
    for (const [, callback] of pending) callback(state.now);
    return pending.length;
  };
  state.restore = () => {
    for (const [name, value] of Object.entries(saved)) {
      if (value === undefined) delete globalThis[name];
      else globalThis[name] = value;
    }
  };

  return state;
}

describe("the engine's lifecycle", () => {
  test("destroy leaves no loop, observer or listener behind", () => {
    const dom = installDom();
    try {
      const engine = createEngine(dom.canvas, dom.host, {
        effect: effectById("jellyfish"),
        intensity: intensityById("medium"),
      });

      assert.equal(dom.flush(), 1, "one loop is running");
      assert.equal(dom.resizeObservers, 1);
      assert.equal(dom.mutationObservers, 1);
      assert.equal(dom.visibilityListeners, 1);
      assert.equal(dom.motionListeners, 1);

      engine.destroy();

      assert.equal(dom.flush(), 0, "the loop is cancelled, not merely ignored");
      assert.equal(dom.resizeObservers, 0, "resize observer leaked");
      assert.equal(dom.mutationObservers, 0, "theme observer leaked");
      assert.equal(dom.visibilityListeners, 0, "visibility listener leaked");
      assert.equal(dom.motionListeners, 0, "reduced-motion listener leaked");
    } finally {
      dom.restore();
    }
  });

  test("switching through every background never runs two loops at once", () => {
    // jellyfish -> stars -> rain -> waves -> none -> jellyfish, the way clicking
    // down the picker and back to the top does it.
    const dom = installDom();
    try {
      const order = ["jellyfish", "stars", "rain", "waves", "none", "jellyfish"];
      let engine = null;

      for (const id of order) {
        if (engine) engine.destroy();
        const effect = effectById(id);
        engine = effect
          ? createEngine(dom.canvas, dom.host, { effect, intensity: intensityById("vivid") })
          : null;

        const running = dom.flush();
        assert.equal(running, effect ? 1 : 0, `${id} should have ${effect ? "one" : "no"} loop`);
        // A few more frames, since a leak shows up as growth rather than as a
        // wrong first count.
        for (let i = 0; i < 3; i += 1) {
          assert.equal(dom.flush(), effect ? 1 : 0, `${id} grew a second loop`);
        }
        assert.equal(dom.resizeObservers, effect ? 1 : 0, `${id} leaked a resize observer`);
        assert.equal(dom.mutationObservers, effect ? 1 : 0, `${id} leaked a theme observer`);
      }

      if (engine) engine.destroy();
      assert.equal(dom.flush(), 0);
      assert.equal(dom.resizeObservers, 0);
      assert.equal(dom.mutationObservers, 0);
      assert.equal(dom.visibilityListeners, 0);
      assert.equal(dom.motionListeners, 0);
    } finally {
      dom.restore();
    }
  });

  test("every effect draws without throwing, at every intensity", () => {
    const dom = installDom();
    try {
      for (const id of EFFECT_IDS) {
        for (const level of INTENSITIES) {
          const engine = createEngine(dom.canvas, dom.host, {
            effect: effectById(id),
            intensity: level,
          });
          const before = dom.draws;
          for (let i = 0; i < 5; i += 1) dom.flush();
          assert.ok(dom.draws > before, `${id} at ${level.id} drew nothing`);
          engine.destroy();
        }
      }
    } finally {
      dom.restore();
    }
  });

  test("reduced motion paints one frame and never starts the loop", () => {
    const dom = installDom({ reduceMotion: true });
    try {
      const engine = createEngine(dom.canvas, dom.host, {
        effect: effectById("waves"),
        intensity: intensityById("medium"),
      });

      assert.ok(dom.draws > 0, "a still field is drawn rather than a blank panel");
      assert.equal(dom.frames.size, 0, "no frame is scheduled");
      assert.equal(dom.flush(), 0);

      engine.destroy();
      assert.equal(dom.motionListeners, 0);
    } finally {
      dom.restore();
    }
  });

  test("a hidden tab does not animate", () => {
    const dom = installDom({ hidden: true });
    try {
      const engine = createEngine(dom.canvas, dom.host, {
        effect: effectById("stars"),
        intensity: intensityById("medium"),
      });

      assert.equal(dom.frames.size, 0, "nothing is scheduled while hidden");
      engine.destroy();
    } finally {
      dom.restore();
    }
  });

  test("the device pixel ratio is capped at two", () => {
    // The fake reports 3, which on a full-panel gradient is more than twice the
    // fill rate of 2 for no visible gain.
    const dom = installDom();
    try {
      const engine = createEngine(dom.canvas, dom.host, {
        effect: effectById("rain"),
        intensity: intensityById("medium"),
      });

      assert.equal(dom.canvas.width, 1600, "800 CSS px at 2x, not 3x");
      assert.equal(dom.canvas.height, 1200);
      assert.equal(dom.canvas.style.width, "800px");
      engine.destroy();
    } finally {
      dom.restore();
    }
  });

  test("an effect that is missing yields an engine that does nothing", () => {
    const dom = installDom();
    try {
      const engine = createEngine(dom.canvas, dom.host, { effect: undefined, intensity: {} });

      assert.equal(dom.resizeObservers, 0, "nothing is observed for a background that cannot draw");
      assert.equal(dom.frames.size, 0);
      engine.destroy();
    } finally {
      dom.restore();
    }
  });
});

/**
 * The glass.
 *
 * Four ways this stops working without anything erroring. The layer going back
 * to `position: absolute` leaves it inside `.neo-main`'s flex track, so the
 * sidebar's glass shows the flat window colour and the effect looks broken
 * rather than absent. The fills losing their `[data-chat-bg]` gate charge every
 * profile a full-surface blur to reveal a solid colour underneath. A fill
 * hard-coding a colour instead of mixing a token looks right in the default
 * palette and wrong in the other six -- Paper especially, where dark glass over
 * paper reads as a smudge. And anything on `.neo-main` that creates a containing
 * block silently re-anchors both the fixed canvas and the fixed composer to that
 * element, dropping the composer out of the window corner.
 */
/**
 * What an effect actually paints, computed from its own draw calls.
 *
 * Rasterises one frame into the same 12-CSS-pixel grid the engine reduces into,
 * accumulating alpha x area per cell -- which is what an energy-conserving
 * downscale computes. `coverage` is the mean alpha over the whole field, which
 * is what decides whether an effect needs a diffusion layer at all; `alphas`
 * applies a gain and reports what the material above would have to work with.
 *
 * Having this here is what lets the tests ask whether the animation survives the
 * glass, rather than asserting a CSS opacity somebody picked.
 */
const GRID = 12;

function fieldEnergy(effect, intensity, width = 2000, height = 1200) {
  const cols = Math.round(width / GRID);
  const rows = Math.round(height / GRID);
  const cells = new Float64Array(cols * rows);
  const alphaOf = (style) => {
    if (style && typeof style === "object" && style.__stops) {
      const stops = style.__stops.map(alphaOf);
      return stops.length ? stops.reduce((a, b) => a + b, 0) / stops.length : 0;
    }
    const found = String(style).match(/rgba?\([^)]*,\s*([\d.]+)\s*\)/);
    return found ? Number(found[1]) : 0;
  };
  const deposit = (x, y, energy) => {
    const col = Math.floor(x / GRID);
    const row = Math.floor(y / GRID);
    if (col < 0 || row < 0 || col >= cols || row >= rows) return;
    cells[row * cols + col] += energy;
  };
  const box = (x, y, w, h, alpha) => {
    for (let row = Math.floor(y / GRID); row <= Math.floor((y + h) / GRID); row += 1) {
      for (let col = Math.floor(x / GRID); col <= Math.floor((x + w) / GRID); col += 1) {
        const overlapX = Math.min((col + 1) * GRID, x + w) - Math.max(col * GRID, x);
        const overlapY = Math.min((row + 1) * GRID, y + h) - Math.max(row * GRID, y);
        if (overlapX > 0 && overlapY > 0) deposit(col * GRID, row * GRID, alpha * overlapX * overlapY);
      }
    }
  };

  let path = [];
  const gradient = () => ({ __stops: [], addColorStop(_, colour) { this.__stops.push(colour); } });
  const ctx = {
    lineWidth: 1, lineCap: "butt", strokeStyle: "", fillStyle: "",
    globalAlpha: 1, globalCompositeOperation: "source-over",
    createLinearGradient: gradient, createRadialGradient: gradient,
    beginPath() { path = []; }, closePath() {},
    moveTo(x, y) { path.push([x, y]); }, lineTo(x, y) { path.push([x, y]); },
    bezierCurveTo(a, b, c, d, x, y) { path.push([a, b], [c, d], [x, y]); },
    quadraticCurveTo(a, b, x, y) { path.push([a, b], [x, y]); },
    arc(x, y, r) { path.push([x - r, y - r], [x + r, y + r]); },
    save() {}, restore() {}, translate() {}, rotate() {}, scale() {},
    clearRect() {}, setTransform() {},
    fillRect(x, y, w, h) { box(x, y, w, h, alphaOf(this.fillStyle) * this.globalAlpha); },
    stroke() {
      const alpha = alphaOf(this.strokeStyle) * this.globalAlpha;
      for (let i = 1; i < path.length; i += 1) {
        const [x0, y0] = path[i - 1];
        const [x1, y1] = path[i];
        const length = Math.hypot(x1 - x0, y1 - y0);
        const steps = Math.max(1, Math.ceil(length));
        for (let step = 0; step < steps; step += 1) {
          const along = (step + 0.5) / steps;
          deposit(x0 + (x1 - x0) * along, y0 + (y1 - y0) * along,
            (alpha * this.lineWidth * length) / steps);
        }
      }
    },
    fill() {
      if (!path.length) return;
      const xs = path.map((point) => point[0]);
      const ys = path.map((point) => point[1]);
      const x = Math.min(...xs);
      const y = Math.min(...ys);
      //: A bell or a dot fills well under its bounding box, and the radial
      //: gradients inside them fade outwards; 0.6 keeps the estimate honest
      //: rather than reporting a square of paint where there is a curve.
      box(x, y, Math.max(...xs) - x, Math.max(...ys) - y, alphaOf(this.fillStyle) * this.globalAlpha * 0.6);
    },
  };

  const palette = {
    accent: [57, 255, 20], bg: [10, 10, 10], ink: [255, 255, 255],
    isLight: false, glowMode: "lighter", rgba,
  };
  effect.create({ width, height, palette, intensity }).frame(ctx, 1 / 60, 0);

  const total = cells.reduce((sum, energy) => sum + energy, 0);
  return {
    coverage: total / (width * height),
    alphas(gain) {
      return [...cells]
        .map((energy) => Math.min(1, (energy / (GRID * GRID)) * gain))
        .filter((alpha) => alpha > 0.002)
        .sort((a, b) => a - b);
    },
  };
}

/** A canvas whose context records what was drawn into it, and how. */
function recordingCanvas() {
  const calls = [];
  const context = {
    globalAlpha: 1, globalCompositeOperation: "source-over",
    imageSmoothingEnabled: false, imageSmoothingQuality: "low",
    clearRect() { calls.push({ op: "clear" }); },
    drawImage(source, ...rest) {
      calls.push({
        op: "draw",
        source,
        args: rest,
        composite: this.globalCompositeOperation,
        alpha: this.globalAlpha,
      });
    },
  };
  return { style: {}, width: 0, height: 0, getContext: () => context, calls, context };
}

//: The three surfaces, and where each one's material is declared. The strip's
//: lives on a pseudo-element because a `backdrop-filter` is uniform across its
//: element and the strip spans the window: on the strip itself it would draw a
//: seam across the field where the blur begins, so the material is masked in
//: instead, and the mask has to miss the card inside.
/**
 * Walk the material's own filter chain, in every palette the stylesheet ships.
 *
 * Every input is read out of the sheet -- the palette anchors, the tint, the
 * saturation and the lift -- so this measures the material as configured rather
 * than a copy of it. What it answers is the question the glass could plausibly
 * get wrong and no test on an alpha would catch: whether dim text on a
 * translucent panel keeps its contrast when the animation moves brightly behind
 * it, in the light palette as well as the dark ones.
 */
function anchorsFor(selector) {
  const block = ruleFor(selector);
  const read = (name) => {
    const found = block.match(new RegExp(`${name}:\\s*(#[0-9a-fA-F]{3,6})`));
    return found ? parseHex(found[1]) : null;
  };
  return { bg: read("--neo-bg"), sidebar: read("--neo-sidebar"), accent: read("--neo-accent"), ink: read("--neo-ink") };
}

function throughGlass({ bg, sidebar, accent }, { tint, sat, lift }, alpha) {
  const luma = (c) => 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  const over = (fg, ground, a) => fg.map((v, i) => a * v + (1 - a) * ground[i]);
  let backdrop = over(accent, bg, alpha);
  const mid = luma(backdrop);
  backdrop = backdrop.map((v) => (mid + sat * (v - mid)) * lift);
  return over(sidebar, backdrop, tint).map((v) => Math.max(0, Math.min(255, v)));
}

function contrastRatio(a, b) {
  const rel = (c) => {
    const [r, g, blue] = c.map((v) => {
      const channel = v / 255;
      return channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * blue;
  };
  const [high, low] = [rel(a), rel(b)].sort((x, y) => y - x);
  return (high + 0.05) / (low + 0.05);
}

/** Every declaration of the wash: the base rule, plus its reduced-motion form. */
function washRules() {
  return rulesMentioning(".chat-bg-layer::before");
}

/** The one that paints it. */
function washRule() {
  const painting = washRules().filter(({ body }) => /background:/.test(body));
  assert.equal(painting.length, 1, "the wash is painted by more than one rule");
  return painting[0].body;
}

const GLASS = [
  { name: "sidebar", selector: "[data-chat-bg] .neo-sidebar" },
  { name: "strip", selector: "[data-chat-bg] .chat-input-wrap::before" },
  { name: "card", selector: "[data-chat-bg] .chat-input-shell" },
];

/**
 * The material contract.
 *
 * Not a list of blessed opacities. The first attempt at this passed every
 * "is it translucent" check and still looked like an opaque panel, because the
 * thing being asked about was the panel rather than what was behind it. So the
 * tests here are about the relationship: there has to be content on the field,
 * the filters have to be configured to act on it, and the surface has to stay
 * thin enough and unmasked enough that it reaches the eye.
 */
describe("the chat glass material", () => {
  test("the field reaches the whole window, not just the chat column", () => {
    // Fixed, or the sidebar's material has nothing behind it but flat colour.
    const layer = ruleFor(".chat-bg-layer");
    assert.match(layer, /position:\s*fixed/);
    assert.doesNotMatch(layer, /position:\s*absolute/);
  });

  test("the diffusion buffer paints under the marks, not over them", () => {
    const markup = renderToStaticMarkup(
      createElement(ChatBackground, { background: "rain", intensity: "medium" }),
    );
    const diffusion = markup.indexOf("chat-bg-diffusion");
    const marks = markup.indexOf("chat-bg-marks");
    assert.ok(diffusion > -1, "no diffusion canvas in the layer");
    assert.ok(marks > -1, "no marks canvas in the layer");
    assert.ok(diffusion < marks, "the diffusion buffer would paint over the effect");
    // Overlaid rather than stacked in flow, or the layer is twice as tall as
    // the window and the marks sit below the fold.
    assert.match(ruleFor(".chat-bg-layer canvas"), /position:\s*absolute/);
  });

  test("diffusion is asked for by the effects that cannot do without it", () => {
    // The declaration has to follow the measurement rather than a preference.
    // An effect thin enough that a blur resolves its marks to nothing needs the
    // layer; one that already paints a broad field does not, and giving it one
    // buys a canvas and a downscale a frame to soften something already soft.
    const SPARSE = 0.005;
    for (const id of EFFECT_IDS) {
      const effect = effectById(id);
      const { coverage } = fieldEnergy(effect, intensityById("medium"));
      const gain = effect.bloom;
      if (coverage < SPARSE) {
        assert.ok(gain > 1, `${id} covers ${(coverage * 100).toFixed(4)}% and asks for no diffusion`);
        assert.ok(gain <= 16, `${id} asks for a gain of ${gain}`);
      } else {
        assert.ok(
          !gain || gain <= 1,
          `${id} covers ${(coverage * 100).toFixed(2)}% and needs no diffusion, but asks for ${gain}`,
        );
      }
    }
  });

  test("diffusion stays visible, and stays modulated rather than flattening", () => {
    // The acceptance criterion, as a number, at every intensity. Too faint and
    // the material is a tinted box over an empty field. Flattened and the layer
    // has stopped tracking the animation, which is the same failure wearing a
    // brighter coat -- so the bound is on how much of the lit field may saturate,
    // not on whether a single cell does: the core of a jellyfish bell reading as
    // white-hot is what a luminous body looks like, a field of them is a blob.
    for (const id of EFFECT_IDS) {
      const effect = effectById(id);
      if (!(effect.bloom > 1)) continue;
      for (const level of INTENSITIES) {
        const alphas = fieldEnergy(effect, level).alphas(effect.bloom);
        assert.ok(alphas.length > 4, `${id}/${level.id} diffuses into almost no cells`);
        const peak = alphas.at(-1);
        assert.ok(
          peak >= 0.03,
          `${id}/${level.id} peaks at alpha ${peak.toFixed(3)}, too faint to survive a blur`,
        );
        const flat = alphas.filter((alpha) => alpha >= 0.995).length / alphas.length;
        assert.ok(flat <= 0.1, `${id}/${level.id} flattens ${(flat * 100).toFixed(0)}% of its lit cells`);
      }
    }
  });

  test("the marks stay the brighter half of what a sparse field paints", () => {
    // Where "rain must still read as rain" lives. The diffusion is a glow under
    // the strokes, so the typical cell it lights has to stay well below the
    // alpha the strokes themselves carry -- lift it until the median cell is as
    // bright as a drop and the drops are wearing halos.
    for (const id of EFFECT_IDS) {
      const effect = effectById(id);
      if (!(effect.bloom > 1)) continue;
      const alphas = fieldEnergy(effect, intensityById("vivid")).alphas(effect.bloom);
      const median = alphas[Math.floor(alphas.length / 2)];
      assert.ok(
        median <= 0.12,
        `${id} lights its median cell to alpha ${median.toFixed(3)}, which reads as a glow cloud`,
      );
    }
  });

  test("the field has continuous structure for the glass to work on", () => {
    // The marks cannot supply it. Total painted energy is fixed, and that budget
    // can be concentrated (0.3% of the diffusion grid lit, for Stars) or spread
    // (every cell at alpha 0.01) but not both -- so a sixth of the window has
    // nothing behind it at any moment, which is exactly the sixth the sidebar
    // covers. The wash is the continuous half, and without it thin panels reveal
    // flat colour rather than a field.
    const wash = washRule();
    const stops = [...wash.matchAll(/var\(--neo-accent\) (\d+)%/g)].map(([, pct]) => Number(pct));
    assert.ok(stops.length >= 2, "the wash needs more than one gradient to read as a field");
    for (const pct of stops) {
      // Additive light under the marks, not a scrim over them.
      assert.ok(pct <= 12, `a wash gradient at ${pct}% accent is a tint on the field, not light in it`);
    }
    assert.match(wash, /animation:\s*neo-field-drift/, "a still wash cannot interact with anything");
    assert.doesNotMatch(wash, /#[0-9a-f]{3}|rgba?\(/i);
  });

  test("the wash stills for reduced motion instead of vanishing", () => {
    // Same contract the engine keeps with the marks: standing still, not gone.
    // Removing it would leave every panel flat for anyone who asked for less
    // movement, which is the one setting that must not cost the interface its
    // material.
    const stilled = washRules().filter(({ body }) => /animation:\s*none/.test(body));
    assert.equal(stilled.length, 1, "the wash does not still under reduced motion");
    const preamble = CSS.slice(0, CSS.indexOf("animation: none"));
    assert.match(preamble.slice(-240), /prefers-reduced-motion: reduce/);
    // And it must not be hidden outright.
    for (const { body } of washRules()) {
      assert.doesNotMatch(body, /display:\s*none|opacity:\s*0/);
    }
  });

  test("a pane carrying text deepens its backdrop; a pane that is an object lifts", () => {
    // The one deliberate non-uniformity, and the reason the sidebar can be this
    // thin at all: brightness above 1 under dim navigation costs a quarter of
    // its contrast, brightness below 1 gives most of that back and `saturate`
    // keeps the colour, so the field still reads through. It inverts on Paper,
    // where the text is the dark thing.
    for (const [scope, expectQuietBelow] of [["[data-chat-bg]", true], ['[data-theme="paper"][data-chat-bg]', false]]) {
      const rule = ruleFor(scope);
      const lift = Number(rule.match(/--glass-lift:\s*([\d.]+)/)[1]);
      const quiet = Number(rule.match(/--glass-lift-quiet:\s*([\d.]+)/)[1]);
      assert.ok(
        expectQuietBelow ? quiet < 1 && lift > 1 : quiet > 1 && lift < 1,
        `${scope}: lift ${lift} and quiet lift ${quiet} do not straddle 1`,
      );
    }
    // The surface that carries the navigation is the one that uses it.
    assert.match(ruleFor("[data-chat-bg] .neo-sidebar"), /brightness\(var\(--glass-lift-quiet\)\)/);
    assert.match(ruleFor("[data-chat-bg] .chat-input-shell"), /brightness\(var\(--glass-lift\)\)/);
  });

  test("the material is one substance, not three unrelated boxes", () => {
    // Shared tokens are what make the three read as the same glass. A surface
    // that hard-codes its own saturation has left the system.
    for (const { name, selector } of GLASS) {
      const rule = ruleFor(selector);
      assert.match(rule, /saturate\(var\(--glass-sat\)\)/, `${name} saturates on its own terms`);
    }
    for (const token of ["--glass-sat", "--glass-lift", "--glass-lift-quiet", "--glass-edge", "--glass-inner", "--glass-inner-lit"]) {
      assert.match(ruleFor("[data-chat-bg]"), new RegExp(`${token}:`), `${token} is undeclared`);
    }
  });

  test("backdrop filtering is configured, both prefixed and not", () => {
    for (const { name, selector } of GLASS) {
      const rule = ruleFor(selector);
      assert.match(rule, /-webkit-backdrop-filter:\s*blur\(/, `${name} has no prefixed filter`);
      assert.match(rule, /(?<!-webkit-)backdrop-filter:\s*blur\(/, `${name} has no filter`);
    }
  });

  test("the material stays translucent and lays down no opaque scrim", () => {
    for (const { name, selector } of GLASS) {
      const fill = ruleFor(selector).match(/background:[^;]+;/s);
      assert.ok(fill, `${name} has no fill of its own`);
      // Mixed against a theme token and towards `transparent`: an opaque
      // `var(--neo-bg)` or a literal colour would be a scrim, not a tint.
      assert.match(fill[0], /color-mix\(in srgb, var\(--neo-[a-z-]+\) \d+%, transparent\)/);
      assert.doesNotMatch(fill[0], /#[0-9a-f]{3}|rgba?\(/i);
      const stops = [...fill[0].matchAll(/var\(--neo-[a-z-]+\) (\d+)%/g)].map(([, pct]) => Number(pct));
      for (const pct of stops) {
        assert.ok(pct <= 15, `${name} tints to ${pct}%, which is a panel rather than a tint`);
      }
    }
  });

  test("the strip's material fades in rather than ending in a seam", () => {
    const strip = ruleFor("[data-chat-bg] .chat-input-wrap::before");
    assert.match(strip, /-webkit-mask-image:\s*linear-gradient/);
    assert.match(strip, /(?<!-webkit-)mask-image:\s*linear-gradient/);
    // On the pseudo-element, or the mask takes the card's edges with it.
    assert.doesNotMatch(ruleFor("[data-chat-bg] .chat-input-wrap"), /mask-image:/);
    // And the strip is the weakest of the three, by blur and by tint.
    const blurOf = (selector) => Number(ruleFor(selector).match(/(?<!-webkit-)backdrop-filter:[^;]*blur\((\d+)px\)/)[1]);
    assert.ok(blurOf(GLASS[1].selector) < blurOf(GLASS[2].selector), "the strip should diffuse less than the card");
    assert.ok(blurOf(GLASS[2].selector) < blurOf(GLASS[0].selector), "the card should diffuse less than the sidebar");
  });

  test("the material is gated on a running background", () => {
    // Ungated, every profile pays a full-surface filter pass to reveal a solid
    // colour: on "none" there is no diffusion buffer and nothing to see.
    for (const { selector, body } of RULES) {
      if (!/backdrop-filter:/.test(body)) continue;
      if (!/\.neo-sidebar|\.chat-input-shell|\.chat-input-wrap/.test(selector)) continue;
      assert.match(selector, /\[data-chat-bg\]/, `ungated filter on ${selector}`);
    }
  });

  test("both light and dark grounds get their own material", () => {
    // Paper is the one light palette, and "part the diffused field from the
    // ground" inverts on it: lift on near-black, deepen on near-white. An edge
    // mixed from `--neo-ink` follows the palette on its own, but brightness
    // cannot.
    const dark = Number(ruleFor("[data-chat-bg]").match(/--glass-lift:\s*([\d.]+)/)[1]);
    const light = Number(ruleFor('[data-theme="paper"][data-chat-bg]').match(/--glass-lift:\s*([\d.]+)/)[1]);
    assert.ok(dark > 1, `dark themes should lift the field, not ${dark}`);
    assert.ok(light < 1, `Paper should deepen the field, not ${light}`);
    for (const token of ["--glass-edge", "--glass-inner", "--glass-inner-lit"]) {
      assert.match(ruleFor("[data-chat-bg]"), new RegExp(`${token}: color-mix\\(in srgb, var\\(--neo-ink\\)`));
    }
  });

  test("the sidebar's dimmest text keeps its contrast in every palette", () => {
    // The material's real accessibility risk, and the one an alpha check cannot
    // see: the sidebar carries `--neo-fg-14` captions, and a translucent panel
    // means their ground now moves with the animation. So the test is about the
    // change rather than the absolute -- these captions are quiet by design, and
    // the glass may not make them meaningfully quieter than the opaque panel did.
    const material = ruleFor("[data-chat-bg]");
    const tint = Number(ruleFor("[data-chat-bg] .neo-sidebar")
      .match(/background: color-mix\(in srgb, var\(--neo-sidebar\) (\d+)%/)[1]) / 100;
    //: The faintest text the sidebar puts on the field, straight from the ramp.
    const dimmest = Number(CSS.match(/--neo-fg-14: color-mix\(in srgb, var\(--neo-ink\) ([\d.]+)%/)[1]) / 100;
    //: The brightest cell any diffusion buffer reaches, softened by the
    //: sidebar's blur being wider than the buffer's own grain.
    const brightest = 0.10 * 0.65;

    const palettes = [":root", ...[...CSS.matchAll(/\[data-theme="([a-z]+)"\] \{/g)].map(([, id]) => `[data-theme="${id}"]`)];
    for (const palette of palettes) {
      const anchors = anchorsFor(palette);
      if (!anchors.bg || !anchors.sidebar || !anchors.accent || !anchors.ink) continue;
      const light = luminance(anchors.bg) > 0.5;
      const scope = light ? '[data-theme="paper"][data-chat-bg]' : "[data-chat-bg]";
      const knobs = {
        tint,
        sat: Number(ruleFor(scope).match(/--glass-sat:\s*([\d.]+)/)[1]),
        lift: Number(ruleFor(scope).match(/--glass-lift:\s*([\d.]+)/)[1]),
      };
      const text = anchors.ink.map((v, i) => dimmest * v + (1 - dimmest) * anchors.bg[i]);

      const opaque = contrastRatio(text, anchors.sidebar);
      const lit = contrastRatio(text, throughGlass(anchors, knobs, brightest));
      const bare = contrastRatio(text, throughGlass(anchors, knobs, 0));
      const worst = Math.min(lit, bare);
      assert.ok(
        worst >= opaque * 0.8,
        `${palette}: caption contrast falls from ${opaque.toFixed(2)}:1 on the opaque panel ` +
        `to ${worst.toFixed(2)}:1 through the glass`,
      );
      // And it must not swing as the animation passes, or the text shimmers.
      assert.ok(
        Math.abs(lit - bare) / bare <= 0.25,
        `${palette}: contrast swings ${((Math.abs(lit - bare) / bare) * 100).toFixed(0)}% as the field moves`,
      );
    }
  });

  test("the composer still answers focus through the material", () => {
    // The material and `:focus-within` weigh the same and the material is
    // further down the sheet, so a `box-shadow` there swallows the focus glow.
    if (!/box-shadow:/.test(ruleFor("[data-chat-bg] .chat-input-shell"))) return;
    const focused = ruleFor("[data-chat-bg] .chat-input-shell:focus-within");
    assert.match(focused, /box-shadow:/);
    assert.match(focused, /--neo-accent-a10/);
    // And it stays part of the material rather than growing its own mixes.
    assert.match(focused, /var\(--glass-inner-lit\)/);
  });

  test("the buffer is rebuilt from the marks canvas every frame", () => {
    const dom = installDom();
    const buffer = recordingCanvas();
    try {
      const engine = createEngine(dom.canvas, dom.host, {
        effect: effectById("rain"),
        intensity: intensityById("medium"),
        bloom: buffer,
      });

      // Sized in CSS pixels and coarse: the host is 800x600 and the device
      // pixel ratio is 3 here, so a buffer that came back 200 wide would have
      // been scaled with the marks canvas and would defeat the reduction.
      assert.equal(buffer.width, Math.round(800 / 12));
      assert.equal(buffer.height, Math.round(600 / 12));

      //: `start` only schedules the first frame, so nothing has been drawn yet.
      assert.equal(dom.flush(), 1);
      const frames = buffer.calls.filter((call) => call.op === "clear").length;
      assert.ok(frames >= 1, "the buffer was never cleared, so gain would compound");
      const fromField = buffer.calls.filter((call) => call.op === "draw" && call.source === dom.canvas);
      const fromSelf = buffer.calls.filter((call) => call.op === "draw" && call.source === buffer);
      assert.equal(fromField.length, frames, "the field should be reduced once per frame");
      assert.ok(fromSelf.length > 0, "no amplification passes at all");
      for (const call of fromSelf) {
        assert.equal(call.composite, "lighter", "amplification has to add, not replace");
      }

      engine.destroy();
    } finally {
      dom.restore();
    }
  });

  test("the gain that reaches the buffer is the one the effect asked for", () => {
    // Each self-draw at alpha a multiplies what is there by (1 + a), so the
    // passes multiply out to the declared gain -- and a gain of 10 arrives as 10
    // rather than being rounded to the nearest power of two.
    for (const id of EFFECT_IDS) {
      const dom = installDom();
      const buffer = recordingCanvas();
      try {
        const effect = effectById(id);
        const engine = createEngine(dom.canvas, dom.host, {
          effect,
          intensity: intensityById("medium"),
          bloom: buffer,
        });
        //: One flush is one paint, so every call recorded belongs to one frame.
        dom.flush();
        const drew = buffer.calls.filter((call) => call.op === "draw");
        if (!(effect.bloom > 1)) {
          // Handed a buffer it did not ask for, an effect that needs none must
          // still leave it alone: the pipeline is off, not merely unused.
          assert.equal(drew.length, 0, `${id} needs no diffusion but the buffer was written`);
          assert.equal(buffer.width, 0, `${id} needs no diffusion but the buffer was sized`);
        } else {
          const gain = drew
            .filter((call) => call.source === buffer)
            .reduce((total, call) => total * (1 + call.alpha), 1);
          assert.ok(
            Math.abs(gain - effect.bloom) < 0.02,
            `${id} asked for ${effect.bloom} and the buffer got ${gain.toFixed(2)}`,
          );
        }
        engine.destroy();
      } finally {
        dom.restore();
      }
    }
  });

  test("an effect that needs no diffusion mounts no second canvas", () => {
    // The saving is the point: no element, no reduction, no upscale per frame.
    const withLayer = renderToStaticMarkup(
      createElement(ChatBackground, { background: "rain", intensity: "medium" }),
    );
    const without = renderToStaticMarkup(
      createElement(ChatBackground, { background: "waves", intensity: "medium" }),
    );
    assert.match(withLayer, /chat-bg-diffusion/);
    assert.doesNotMatch(without, /chat-bg-diffusion/);
    assert.match(without, /chat-bg-marks/, "the field itself still has to render");
  });

  test("a stilled field still gets its material", () => {
    // Reduced motion paints one frame and starts no loop. The diffusion runs
    // inside that paint, so the glass has something behind it standing still --
    // without it the material would collapse to a tinted box for anyone who
    // asked for less movement.
    const dom = installDom({ reduceMotion: true });
    const buffer = recordingCanvas();
    try {
      const engine = createEngine(dom.canvas, dom.host, {
        effect: effectById("stars"),
        intensity: intensityById("medium"),
        bloom: buffer,
      });
      assert.equal(dom.flush(), 0, "reduced motion should start no loop");
      assert.ok(
        buffer.calls.some((call) => call.op === "draw" && call.source === dom.canvas),
        "the still frame was never reduced into the buffer",
      );
      engine.destroy();
    } finally {
      dom.restore();
    }
  });

  test("an engine with no buffer still runs the field", () => {
    // Every caller that predates the diffusion layer passes one canvas, and a
    // missing buffer has to cost the glass its material and nothing else.
    const dom = installDom();
    try {
      const engine = createEngine(dom.canvas, dom.host, {
        effect: effectById("waves"),
        intensity: intensityById("medium"),
      });
      assert.equal(dom.flush(), 1, "the loop should run without a diffusion buffer");
      engine.destroy();
    } finally {
      dom.restore();
    }
  });

  test("nothing on .neo-main becomes a containing block", () => {
    // Both the field and the composer are fixed to the viewport, and either
    // would re-anchor to `.neo-main` if it gained a filter or a transform.
    const trapping = /backdrop-filter:|(?<!backdrop-)filter:|transform:|perspective:|will-change:|contain:/;
    for (const { selector, body } of rulesMentioning(".neo-main")) {
      assert.doesNotMatch(body, trapping, `${selector} would trap the composer`);
    }
  });
});
