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
import { describe, test } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import BackgroundSettings from "../src/BackgroundSettings.jsx";
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
