/**
 * Fetching panel code ahead of the open, without getting in the way.
 *
 * Intent (a pointer over, focus on or press of a panel's trigger) fetches at
 * once; the sidebar's destinations are fetched one at a time in idle time,
 * paused while the person is busy or the page is hidden, and not at all when
 * the device or network asks for economy. A preloaded panel opens with no
 * loading flash; one that is not keeps its fallback; a failed preload never
 * poisons the real open.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { lazyPanel } from "../src/lazyPanel.jsx";
import { createPanelPreloader, isConstrained } from "../src/panelPreload.js";

/** A panel whose download settles when the test says so. */
function fakePanel() {
  const panel = { loads: 0, loaded: false, settle: null };
  panel.isLoaded = () => panel.loaded;
  panel.preload = () => {
    panel.loads += 1;
    return new Promise((resolve, reject) => {
      panel.settle = {
        ok: () => { panel.loaded = true; resolve(); },
        fail: () => reject(new Error("offline")),
      };
    });
  };
  return panel;
}

/** A manual clock and scheduler: tasks run when the test advances time. */
function harness(env = {}) {
  let time = 0;
  let hidden = false;
  let tasks = [];
  const preloader = createPanelPreloader({
    env,
    isHidden: () => hidden,
    now: () => time,
    schedule: (fn, delay) => {
      const task = { at: time + delay, fn, cancelled: false };
      tasks.push(task);
      return () => { task.cancelled = true; };
    },
  });
  return {
    preloader,
    advance(ms) {
      time += ms;
      const due = tasks.filter((task) => !task.cancelled && task.at <= time);
      tasks = tasks.filter((task) => !due.includes(task));
      for (const task of due) task.fn();
    },
    hide(value) { hidden = value; },
  };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

describe("when to fetch in the background at all", () => {
  test("not when the device or network asks for economy", () => {
    assert.equal(isConstrained({ connection: { saveData: true } }), true);
    assert.equal(isConstrained({ connection: { effectiveType: "2g" } }), true);
    assert.equal(isConstrained({ connection: { effectiveType: "slow-2g" } }), true);
    assert.equal(isConstrained({ deviceMemory: 2 }), true);
    assert.equal(isConstrained({ connection: { effectiveType: "4g" }, deviceMemory: 8 }), false);
    assert.equal(isConstrained({}), false);
  });

  test("a constrained device queues nothing", () => {
    const { preloader, advance } = harness({ connection: { saveData: true } });
    const panel = fakePanel();
    assert.equal(preloader.background([panel]), false);
    advance(10_000);
    assert.equal(panel.loads, 0);
  });
});

describe("background fetching", () => {
  test("starts after a delay and runs one panel at a time", async () => {
    const { preloader, advance } = harness();
    const [a, b] = [fakePanel(), fakePanel()];
    preloader.background([a, b]);
    advance(1_999);
    assert.equal(a.loads, 0, "nothing in the first moments after load");
    advance(1);
    assert.equal(a.loads, 1);
    assert.equal(b.loads, 0, "never two at once");
    a.settle.ok();
    await tick();
    advance(0);
    assert.equal(b.loads, 1);
  });

  test("waits for a quiet moment after input", () => {
    const { preloader, advance } = harness();
    const panel = fakePanel();
    preloader.background([panel]);
    advance(1_500);
    preloader.noteInput();
    advance(500);
    assert.equal(panel.loads, 0, "input half a second ago: not yet");
    advance(1_000);
    assert.equal(panel.loads, 1);
  });

  test("waits while the page is hidden", () => {
    const { preloader, advance, hide } = harness();
    const panel = fakePanel();
    hide(true);
    preloader.background([panel]);
    advance(5_000);
    assert.equal(panel.loads, 0);
    hide(false);
    advance(1_000);
    assert.equal(panel.loads, 1);
  });

  test("a failed download does not stop the queue", async () => {
    const { preloader, advance } = harness();
    const [a, b] = [fakePanel(), fakePanel()];
    preloader.background([a, b]);
    advance(2_000);
    a.settle.fail();
    await tick();
    advance(0);
    assert.equal(b.loads, 1);
  });

  test("stops cleanly", () => {
    const { preloader, advance } = harness();
    const panel = fakePanel();
    preloader.background([panel]);
    preloader.stop();
    advance(10_000);
    assert.equal(panel.loads, 0);
  });
});

describe("intent", () => {
  test("fetches at once, at most two together, and skips what is loaded", async () => {
    const { preloader } = harness();
    const [a, b, c] = [fakePanel(), fakePanel(), fakePanel()];
    assert.equal(preloader.intent(a), true);
    assert.equal(preloader.intent(b), true);
    assert.equal(preloader.intent(c), false, "a third waits for the next hint");
    a.settle.ok();
    await tick();
    assert.equal(preloader.intent(a), false, "already here");
    assert.equal(preloader.intent(c), true);
  });

  test("still works on a constrained device: the person is about to open it", () => {
    const { preloader } = harness({ connection: { saveData: true } });
    const panel = fakePanel();
    assert.equal(preloader.intent(panel), true);
  });
});

describe("a lazy panel", () => {
  const module = { default: () => createElement("p", null, "panel body") };

  test("shows its fallback until its code is here, then renders straight away", async () => {
    let loads = 0;
    const Panel = lazyPanel(() => { loads += 1; return Promise.resolve(module); });
    assert.match(renderToStaticMarkup(createElement(Panel)), /Loading/);
    await Panel.preload();
    assert.equal(Panel.isLoaded(), true);
    const html = renderToStaticMarkup(createElement(Panel));
    assert.match(html, /panel body/);
    assert.doesNotMatch(html, /Loading/);
    await Panel.preload();
    assert.equal(loads, 1, "one download, shared");
  });

  test("a failed preload is retried by the next attempt", async () => {
    let attempts = 0;
    const Panel = lazyPanel(() => {
      attempts += 1;
      return attempts === 1 ? Promise.reject(new Error("offline")) : Promise.resolve(module);
    });
    await assert.rejects(Panel.preload());
    await Panel.preload();
    assert.equal(attempts, 2);
    assert.match(renderToStaticMarkup(createElement(Panel)), /panel body/);
  });
});
