/**
 * Interaction latency and correctness benchmark for the real Neo app.
 *
 * Drives a production build (`npm run build && npm run preview`) in an isolated
 * Chrome over the DevTools Protocol, inside a disposable account-profile seeded
 * with chats by `seed_profile.py`, and reports what a person actually waits for:
 *
 *   - frames from an input to the screen showing its finished result (hover,
 *     press, a selected state, the clicked chat row), measured in the page with
 *     requestAnimationFrame against a calibrated target style;
 *   - Event Timing durations for trusted interactions;
 *   - long tasks and long animation frames while it runs;
 *   - whether a lazy panel showed its loading fallback;
 *   - how many message rows played their entrance animation.
 *
 * API calls can be delayed or failed through Fetch interception, which is what
 * makes optimistic updates and races observable and repeatable.
 *
 * Usage, from `frontend/`, with the backend on :8000 and a preview running:
 *
 *   npm run build && npm run preview -- --port 4173 --strictPort
 *   BENCH_LABEL=after BENCH_CPU=1,4 node bench/interaction.mjs [scenario,...]
 *
 * Environment: BENCH_ORIGIN (default http://127.0.0.1:4173/), BENCH_LABEL,
 * BENCH_CPU (comma list, default "1"), BENCH_REPEATS (default 10), BENCH_CACHE
 * ("warm" or "cold"), BENCH_OUTPUT (JSON path), BENCH_CHROME (any Chromium:
 * Chrome, Brave), BENCH_APP (the app to bring forward), BENCH_PORT,
 * BENCH_WINDOW, BENCH_PYTHON (default ../.venv/bin/python), BENCH_CHATS and
 * BENCH_LONG_MESSAGES (size of the seeded profile), BENCH_MOBILE (412x915,
 * touch, slow 4G), BENCH_COMMITS (React commit probe; development builds only,
 * where component names survive), BENCH_SHOTS (screenshot directory).
 *
 * Scenarios (comma list as the first argument; default: the first twelve):
 *   hover, press, mode         feedback frames for pointer, press, selected state
 *   effort, openChat           click results that wait on a held server reply
 *   typing, panel, arrivals    keystroke timing, first panel open, entrances
 *   openRace, settingsRace,    rapid clicks with delayed and failed requests
 *   sidebarRace, newChatDuringOpen, navDuringRefresh
 *   send                       one real reply from the default model
 *   preload                    panel code fetched on intent and in idle time
 *   commitsTyping, commitsStream  which components render (BENCH_COMMITS)
 *   touchPress, tapPress       finger presses: raw touch events, a tap gesture
 *   visual                     class tokens and screenshots per screen
 *   longTasks                  long tasks and frames over a sweep, a panel, typing
 *
 * The account-profile it creates is deleted through the API when the run ends,
 * whether it passed or failed. Its credentials sit in BENCH_STATE (default
 * <tmpdir>/neo-interaction-profile.json) while the run is going, so a run that
 * dies is cleaned up by the next one.
 */
import { execFile, execFileSync, spawn } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { randomUUID } from "node:crypto";

const run = promisify(execFile);
const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, "../..");

const CHROME = process.env.BENCH_CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PORT = Number(process.env.BENCH_PORT || 9351);
const ORIGIN = process.env.BENCH_ORIGIN || "http://127.0.0.1:4173/";
const WINDOW = process.env.BENCH_WINDOW || "1512,982";
const LABEL = process.env.BENCH_LABEL || "run";
const CPU_RATES = (process.env.BENCH_CPU || "1").split(",").map(Number);
const REPEATS = Number(process.env.BENCH_REPEATS || 10);
const CACHE = process.env.BENCH_CACHE || "warm";
const PYTHON = process.env.BENCH_PYTHON || join(REPO, ".venv/bin/python");
//: A phone: 412 x 915 at 2.625x, touch, and "slow 4G" (150 ms, 1.6 Mbit/s down).
//: Combine with BENCH_CPU=4 for a mid-range device.
const MOBILE = Boolean(process.env.BENCH_MOBILE);
const CHATS = Number(process.env.BENCH_CHATS || 40);
const LONG_CHAT_MESSAGES = Number(process.env.BENCH_LONG_MESSAGES || 120);

//: `panel` runs before `arrivals`, which opens Notes and would make the first open a warm one.
const ALL_SCENARIOS = ["hover", "press", "mode", "effort", "openChat", "typing", "panel", "arrivals", "openRace", "settingsRace", "sidebarRace", "send"];
const SCENARIOS = process.argv[2] ? process.argv[2].split(",") : ALL_SCENARIOS;

//: rAF only runs at full rate for a foreground window, so the benchmark window
//: is activated before measuring (macOS); Page.bringToFront selects the tab.
const APP = process.env.BENCH_APP || (/Brave/.test(process.env.BENCH_CHROME || "") ? "Brave Browser" : "Google Chrome");
function front() {
  try { execFileSync("osascript", ["-e", `tell application "${APP}" to activate`]); }
  catch { /* not fatal: frame counts would show it */ }
}

/* ------------------------------------------------------------------ CDP -- */

function connect(url) {
  const socket = new WebSocket(url);
  const pending = new Map();
  const listeners = new Map();
  let next = 1;
  let closed = null;
  const ready = new Promise((resolveReady, reject) => {
    socket.addEventListener("open", resolveReady, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  //: A dead browser must fail the run (so cleanup runs), never leave a call
  //: waiting on a reply that cannot come.
  const failAll = (reason) => {
    closed = closed || reason;
    for (const slot of pending.values()) { clearTimeout(slot.timer); slot.reject(new Error(`${reason} (${slot.method})`)); }
    pending.clear();
  };
  socket.addEventListener("close", () => failAll("DevTools socket closed"));
  socket.addEventListener("error", () => failAll("DevTools socket error"));
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.method) {
      for (const handler of listeners.get(message.method) || []) handler(message.params);
      return;
    }
    const slot = pending.get(message.id);
    if (!slot) return;
    pending.delete(message.id);
    clearTimeout(slot.timer);
    if (message.error) slot.reject(new Error(`${message.error.message} (${slot.method})`));
    else slot.resolve(message.result);
  });
  const send = (method, params = {}, timeoutMs = 60_000) =>
    new Promise((resolveSend, reject) => {
      if (closed) { reject(new Error(`${closed} (${method})`)); return; }
      const id = next++;
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error(`DevTools call timed out after ${timeoutMs} ms (${method})`));
      }, timeoutMs);
      pending.set(id, { resolve: resolveSend, reject, method, timer });
      socket.send(JSON.stringify({ id, method, params }));
    });
  const on = (method, handler) => {
    if (!listeners.has(method)) listeners.set(method, []);
    listeners.get(method).push(handler);
  };
  return { ready, send, on, close: () => socket.close() };
}

async function evaluate(cdp, expression, timeoutMs = 60_000) {
  const { result, exceptionDetails } = await cdp.send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  }, timeoutMs);
  if (exceptionDetails) {
    throw new Error(exceptionDetails.exception?.description || exceptionDetails.text || "evaluate failed");
  }
  return result.value;
}

async function pageSocket() {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      const page = list.find((target) => target.type === "page");
      if (page?.webSocketDebuggerUrl) return page.webSocketDebuggerUrl;
    } catch { /* Chrome is still starting */ }
    await sleep(250);
  }
  throw new Error("Chrome never offered a page target");
}

/* ------------------------------------------------------- page probes -- */

//: Installed before any app code runs, on every navigation. Everything it
//: records lives on `window.__bench`; the driver reads and resets it per sample.
const INSTRUMENT = String.raw`(() => {
  const bench = window.__bench = {
    events: [], longTasks: [], frames: [], arrivals: 0, fallbacks: 0, lastInput: null,
  };
  const keep = (type, sink, map, extra = {}) => {
    try {
      new PerformanceObserver((list) => { for (const entry of list.getEntries()) sink.push(map(entry)); })
        .observe({ type, buffered: false, ...extra });
    } catch { /* unsupported entry type */ }
  };
  keep("event", bench.events, (e) => ({
    name: e.name, duration: e.duration, interactionId: e.interactionId || 0,
    start: e.startTime, inputDelay: e.processingStart - e.startTime,
    processing: e.processingEnd - e.processingStart,
  }), { durationThreshold: 16 });
  keep("longtask", bench.longTasks, (e) => ({ start: e.startTime, duration: e.duration }));
  keep("long-animation-frame", bench.frames, (e) => ({ start: e.startTime, duration: e.duration, blocking: e.blockingDuration }));
  bench.inputs = [];
  for (const type of ["pointerover", "pointerdown", "keydown", "click"]) {
    addEventListener(type, (event) => {
      if (event.isTrusted) bench.inputs.push({ type, time: event.timeStamp });
    }, { capture: true, passive: true });
  }
  //: Set by the press scenario: the click a measured press produces is
  //: swallowed before React's root listener can see it.
  bench.suppressClicks = false;
  document.addEventListener("click", (event) => {
    if (bench.suppressClicks && event.isTrusted) { event.preventDefault(); event.stopImmediatePropagation(); }
  }, true);
  document.addEventListener("animationstart", (event) => {
    if (event.target?.classList?.contains("neo-chat-message")) bench.arrivals += 1;
  }, true);
  const isFallback = (node) => node.nodeType === 1
    && node.getAttribute?.("role") === "status" && /Loading/.test(node.textContent || "");
  new MutationObserver((records) => {
    for (const record of records) {
      for (const node of record.addedNodes) {
        if (isFallback(node) || (node.nodeType === 1 && [...node.querySelectorAll?.('[role="status"]') || []].some(isFallback))) {
          bench.fallbacks += 1;
        }
      }
    }
  }).observe(document, { childList: true, subtree: true });
  //: Frames from the next trusted input of type \`after\` until predicate()
  //: holds. Counting starts at the first rAF after that input and the time is
  //: measured from the input event's own timestamp, so the wait between
  //: arming the probe and dispatching the input is never included.
  bench.until = (predicate, timeoutMs = 2000, after = null) => new Promise((done) => {
    const armedAt = bench.inputs.length;
    const begun = performance.now();
    let from = null;
    let frames = 0;
    const tick = (now) => {
      if (from === null) {
        const input = bench.inputs.slice(armedAt).find((entry) => !after || entry.type === after);
        if (!input) {
          if (now - begun > timeoutMs) return done({ frames: null, ms: null });
          return requestAnimationFrame(tick);
        }
        from = input.time;
      }
      frames += 1;
      let ok = false;
      try { ok = predicate(); } catch { ok = false; }
      //: performance.now() rather than the frame timestamp: the frame's start
      //: can precede an input handled inside it, which would read as negative.
      const at = performance.now();
      if (ok) return done({ frames, ms: +(at - from).toFixed(1) });
      if (at - from > timeoutMs) return done({ frames, ms: null });
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });
  bench.style = (el) => {
    const s = getComputedStyle(el);
    return [s.backgroundColor, s.color, s.borderTopColor, s.boxShadow, s.filter, s.opacity].join("|");
  };
  bench.reset = () => {
    bench.events.length = 0; bench.longTasks.length = 0; bench.frames.length = 0;
    bench.arrivals = 0; bench.fallbacks = 0; bench.inputs.length = 0;
  };
})();`;

//: React commit probe (development builds, where component names survive).
//: A minimal DevTools hook that React registers with; on each commit it walks
//: the committed tree the way React DevTools does -- descending only where a
//: child pointer changed, so a bailed-out subtree is not counted -- and counts
//: which named components performed work.
const COMMIT_PROBE = String.raw`(() => {
  const PERFORMED_WORK = 1;
  const COMPONENT_TAGS = new Set([0, 1, 11, 14, 15]);
  const nameOf = (fiber) => {
    const type = fiber.type;
    if (!type) return null;
    const raw = type.displayName || type.name || type.type?.displayName || type.type?.name || type.render?.name || null;
    // \`export const X = memo(function X ...)\` compiles the inner function to
    // X2 in development; the trailing number is not part of the name.
    return raw ? raw.replace(/\d+$/, "") : null;
  };
  const probe = window.__commits = { commits: 0, renders: {}, reset() { this.commits = 0; this.renders = {}; } };
  const visit = (next, prev) => {
    if (COMPONENT_TAGS.has(next.tag) && (prev === null || (next.flags & PERFORMED_WORK))) {
      const name = nameOf(next);
      if (name) probe.renders[name] = (probe.renders[name] || 0) + 1;
    }
    if (prev && next.child === prev.child) return;
    for (let child = next.child; child; child = child.sibling) visit(child, child.alternate || null);
  };
  window.__REACT_DEVTOOLS_GLOBAL_HOOK__ = {
    isDisabled: false,
    supportsFiber: true,
    renderers: new Map(),
    inject(renderer) { const id = this.renderers.size + 1; this.renderers.set(id, renderer); return id; },
    onCommitFiberRoot(id, root) {
      probe.commits += 1;
      const current = root.current;
      try { visit(current, current.alternate || null); } catch { /* a shape this probe does not know */ }
    },
    onCommitFiberUnmount() {},
    onPostCommitFiberRoot() {},
    onScheduleFiberRoot() {},
    checkDCE() {},
  };
})();`;

/* ----------------------------------------------------------- helpers -- */

const quantile = (values, q) => {
  const sorted = values.filter((v) => typeof v === "number").sort((a, b) => a - b);
  if (!sorted.length) return null;
  const index = Math.min(sorted.length - 1, Math.max(0, Math.ceil(q * sorted.length) - 1));
  return +sorted[index].toFixed(1);
};
const summary = (values) => ({
  n: values.length,
  median: quantile(values, 0.5),
  p75: quantile(values, 0.75),
  p95: quantile(values, 0.95),
  max: quantile(values, 1),
  missing: values.filter((v) => typeof v !== "number").length,
});

async function centerOf(cdp, selector, index = 0) {
  return evaluate(cdp, `(() => {
    const el = document.querySelectorAll(${JSON.stringify(selector)})[${index}];
    if (!el) return null;
    el.scrollIntoView({ block: "nearest" });
    const r = el.getBoundingClientRect();
    return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
  })()`);
}

const mouse = (cdp, type, x, y, extra = {}) =>
  cdp.send("Input.dispatchMouseEvent", { type, x, y, button: "none", ...extra });

//: A neutral spot over the transcript, away from every control.
async function park(cdp) {
  const spot = await evaluate(cdp, `(() => {
    const shell = document.querySelector(".neo-shell");
    const r = shell ? shell.getBoundingClientRect() : { left: 600, top: 120, width: 200 };
    return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + 60) };
  })()`);
  await mouse(cdp, "mouseMoved", spot.x, spot.y);
}

async function click(cdp, point) {
  await mouse(cdp, "mouseMoved", point.x, point.y);
  await mouse(cdp, "mousePressed", point.x, point.y, { button: "left", clickCount: 1 });
  await mouse(cdp, "mouseReleased", point.x, point.y, { button: "left", clickCount: 1 });
}

async function typeText(cdp, text, gapMs) {
  for (const char of text) {
    await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key: char, text: char });
    await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: char });
    await sleep(gapMs);
  }
}

async function collect(cdp) {
  return evaluate(cdp, `(() => {
    const b = window.__bench;
    return { events: b.events.slice(), longTasks: b.longTasks.slice(), frames: b.frames.slice(),
      arrivals: b.arrivals, fallbacks: b.fallbacks };
  })()`);
}

/* ----------------------------------------------- Fetch interception -- */

//: Rules are { method, path: RegExp, delayMs, fail, nth }. Every paused request
//: is released; only matching ones are delayed or failed (`nth`: only the n-th
//: match, counting from 1, is affected). Each request's start and end are kept,
//: so overlap (two PATCHes for one chat in flight together) can be measured.
function createInterceptor(cdp) {
  let rules = [];
  const counts = new Map();
  const log = [];
  const byNetworkId = new Map();
  const finish = (requestId, outcome) => {
    const entry = byNetworkId.get(requestId);
    if (entry && entry.end === null) { entry.end = Date.now(); entry.outcome = outcome; }
  };
  cdp.on("Network.loadingFinished", ({ requestId }) => finish(requestId, "ok"));
  cdp.on("Network.loadingFailed", ({ requestId, canceled }) => finish(requestId, canceled ? "canceled" : "failed"));
  cdp.on("Fetch.requestPaused", async ({ requestId, request, networkId }) => {
    const { pathname } = new URL(request.url);
    let rule = null;
    for (const candidate of rules) {
      if ((candidate.method && candidate.method !== request.method) || !candidate.path.test(pathname)) continue;
      const seen = (counts.get(candidate) || 0) + 1;
      counts.set(candidate, seen);
      if (!rule && (!candidate.nth || candidate.nth === seen)) rule = candidate;
    }
    const entry = { method: request.method, path: pathname, start: Date.now(), end: null, outcome: null, rule: Boolean(rule) };
    log.push(entry);
    if (networkId) byNetworkId.set(networkId, entry);
    try {
      if (rule?.delayMs) await sleep(rule.delayMs);
      if (rule?.fail) await cdp.send("Fetch.failRequest", { requestId, errorReason: "Failed" });
      else await cdp.send("Fetch.continueRequest", { requestId });
    } catch { /* the page navigated away, or the request was cancelled */ }
  });
  return {
    async set(next) {
      rules = next;
      counts.clear();
      if (next.length) await cdp.send("Fetch.enable", { patterns: [{ urlPattern: "*/api/*", requestStage: "Request" }] });
      else await cdp.send("Fetch.disable");
    },
    log,
    //: Most requests matching (method, path) that were in flight at once.
    maxOverlap(method, path, since = 0) {
      const spans = log.slice(since).filter((e) => e.method === method && path.test(e.path))
        .map((e) => [e.start, e.end ?? Date.now()]);
      let max = 0;
      for (const [start] of spans) max = Math.max(max, spans.filter(([a, b]) => a <= start && start < b).length);
      return max;
    },
  };
}

/* --------------------------------------------------------- scenarios -- */

//: Controls visible on the chat screen at rest, with and without transitions.
const HOVER_TARGETS = [
  ".sidebar-collapse-button",
  ".sidebar-primary-action .neo-button",
  ".composer-menu-button",
  ".chat-mode-switch button:not(.active)",
  ".chat-item:not(.active) .chat-item-title",
  ".system-nav button",
];

async function calibrate(cdp, selector, action) {
  const point = await centerOf(cdp, selector);
  if (!point) return null;
  await park(cdp);
  await sleep(450);
  const rest = await evaluate(cdp, `window.__bench.style(document.querySelector(${JSON.stringify(selector)}))`);
  await action(point);
  await sleep(450);
  const target = await evaluate(cdp, `window.__bench.style(document.querySelector(${JSON.stringify(selector)}))`);
  return { point, rest, target };
}

async function scenarioHover(cdp) {
  const out = {};
  for (const selector of HOVER_TARGETS) {
    const cal = await calibrate(cdp, selector, (p) => mouse(cdp, "mouseMoved", p.x, p.y));
    if (!cal) { out[selector] = { skipped: "not on screen" }; continue; }
    if (cal.rest === cal.target) { out[selector] = { skipped: "no hover style" }; continue; }
    const samples = [];
    for (let i = 0; i < REPEATS; i += 1) {
      await park(cdp);
      await sleep(400);
      const wait = evaluate(cdp, `window.__bench.until(() => window.__bench.style(document.querySelector(${JSON.stringify(selector)})) === ${JSON.stringify(cal.target)}, 2000, "pointerover")`);
      await mouse(cdp, "mouseMoved", cal.point.x, cal.point.y);
      samples.push(await wait);
    }
    out[selector] = { frames: summary(samples.map((s) => s.frames)), ms: summary(samples.map((s) => s.ms)) };
  }
  await park(cdp);
  return out;
}

//: A press is held, measured and released on the same spot, with the click it
//: would produce swallowed in the capture phase, so nothing opens or toggles
//: and the pointer never moves while a button is down (which starts a drag).
async function scenarioPress(cdp) {
  const out = {};
  await evaluate(cdp, "window.__bench.suppressClicks = true");
  try {
    for (const selector of HOVER_TARGETS) {
      const point = await centerOf(cdp, selector);
      if (!point) { out[selector] = { skipped: "not on screen" }; continue; }
      const read = () => evaluate(cdp, `window.__bench.style(document.querySelector(${JSON.stringify(selector)}))`);
      const press = () => mouse(cdp, "mousePressed", point.x, point.y, { button: "left", clickCount: 1 });
      const release = () => mouse(cdp, "mouseReleased", point.x, point.y, { button: "left", clickCount: 1 });
      // Hover first and let it settle, so only the press itself is measured.
      await mouse(cdp, "mouseMoved", point.x, point.y);
      await sleep(450);
      const hovered = await read();
      await press();
      await sleep(450);
      const pressed = await read();
      await release();
      if (hovered === pressed) { out[selector] = { feedback: false }; continue; }
      const samples = [];
      for (let i = 0; i < REPEATS; i += 1) {
        await sleep(350);
        const wait = evaluate(cdp, `window.__bench.until(() => window.__bench.style(document.querySelector(${JSON.stringify(selector)})) === ${JSON.stringify(pressed)}, 2000, "pointerdown")`);
        await press();
        samples.push(await wait);
        await release();
      }
      out[selector] = { feedback: true, frames: summary(samples.map((s) => s.frames)), ms: summary(samples.map((s) => s.ms)) };
    }
  } finally {
    await evaluate(cdp, "window.__bench.suppressClicks = false");
    await park(cdp);
  }
  return out;
}

//: A finger press: touch emulation, so the browser applies \`:active\` the way a
//: phone does. Held, measured and released in place, its click swallowed.
const TOUCH_TARGETS = [".composer-menu-button", ".chat-mode-switch button:not(.active)", ".chat-input-form .send-button"];
async function scenarioTouchPress(cdp) {
  const out = {};
  const touch = (type, point) => cdp.send("Input.dispatchTouchEvent", { type, touchPoints: point ? [{ x: point.x, y: point.y }] : [] });
  await evaluate(cdp, "window.__bench.suppressClicks = true");
  try {
    for (const selector of TOUCH_TARGETS) {
      const point = await centerOf(cdp, selector);
      if (!point) { out[selector] = { skipped: "not on screen" }; continue; }
      const read = () => evaluate(cdp, `window.__bench.style(document.querySelector(${JSON.stringify(selector)}))`);
      await sleep(400);
      const rest = await read();
      await touch("touchStart", point);
      await sleep(450);
      const pressed = await read();
      await touch("touchEnd");
      if (rest === pressed) { out[selector] = { feedback: false }; continue; }
      const samples = [];
      for (let i = 0; i < REPEATS; i += 1) {
        await sleep(400);
        const wait = evaluate(cdp, `window.__bench.until(() => window.__bench.style(document.querySelector(${JSON.stringify(selector)})) === ${JSON.stringify(pressed)}, 2000, "pointerdown")`);
        await touch("touchStart", point);
        samples.push(await wait);
        await touch("touchEnd");
      }
      out[selector] = { feedback: true, frames: summary(samples.map((s) => s.frames)), ms: summary(samples.map((s) => s.ms)) };
    }
  } finally {
    await evaluate(cdp, "window.__bench.suppressClicks = false");
  }
  return out;
}

//: A real tap gesture (what a phone's touchscreen produces, unlike bare touch
//: events), held for 400 ms: does the press dim appear, and how soon after the
//: pointer goes down? Run with BENCH_MOBILE.
async function scenarioTapPress(cdp) {
  const out = {};
  await evaluate(cdp, "window.__bench.suppressClicks = true");
  try {
    for (const selector of TOUCH_TARGETS) {
      const point = await centerOf(cdp, selector);
      if (!point) { out[selector] = { skipped: "not on screen" }; continue; }
      const samples = [];
      for (let i = 0; i < Math.min(REPEATS, 5); i += 1) {
        await sleep(400);
        const watch = evaluate(cdp, `window.__bench.until(() => /opacity\\(0\\.75\\)/.test(getComputedStyle(document.querySelector(${JSON.stringify(selector)})).filter), 1500, "pointerdown")`);
        await cdp.send("Input.synthesizeTapGesture", { x: point.x, y: point.y, duration: 400, tapCount: 1, gestureSourceType: "touch" });
        samples.push(await watch);
      }
      out[selector] = {
        dimmed: samples.filter((sample) => sample.ms !== null).length,
        of: samples.length,
        ms: summary(samples.map((sample) => sample.ms)),
      };
    }
  } finally {
    await evaluate(cdp, "window.__bench.suppressClicks = false");
  }
  return out;
}

//: The Chat/Agent switch: a click-driven selected state with a transition.
async function scenarioMode(cdp) {
  const samples = [];
  const events = [];
  for (let i = 0; i < REPEATS; i += 1) {
    const point = await centerOf(cdp, ".chat-mode-switch button:not(.active)");
    const label = await evaluate(cdp, `document.querySelector(".chat-mode-switch button:not(.active)").textContent`);
    // Final look of the active state, read once the transition has finished.
    await park(cdp);
    await sleep(300);
    await evaluate(cdp, "window.__bench.reset()");
    const wait = evaluate(cdp, `(() => {
      const el = [...document.querySelectorAll(".chat-mode-switch button")].find((b) => b.textContent === ${JSON.stringify(label)});
      return window.__bench.until(() => el.classList.contains("active"), 2000, "click").then(async (cls) => {
        const settled = await new Promise((r) => setTimeout(() => r(window.__bench.style(el)), 400));
        return { cls, settled };
      });
    })()`);
    await click(cdp, point);
    const first = await wait;
    // Measure how long the styled result took by replaying against the settled style.
    const backPoint = await centerOf(cdp, ".chat-mode-switch button:not(.active)");
    await click(cdp, backPoint);
    await park(cdp);
    await sleep(450);
    const again = evaluate(cdp, `(() => {
      const el = [...document.querySelectorAll(".chat-mode-switch button")].find((b) => b.textContent === ${JSON.stringify(label)});
      return window.__bench.until(() => window.__bench.style(el) === ${JSON.stringify(first.settled)}, 2000, "click");
    })()`);
    await click(cdp, point);
    samples.push({ classFrames: first.cls.frames, styled: await again });
    events.push(...(await collect(cdp)).events.filter((e) => e.name === "click" || e.name === "pointerdown"));
    // Leave it where it started for the next sample.
    await click(cdp, await centerOf(cdp, ".chat-mode-switch button:not(.active)"));
    await park(cdp);
    await sleep(300);
  }
  return {
    classFrames: summary(samples.map((s) => s.classFrames)),
    styledFrames: summary(samples.map((s) => s.styled.frames)),
    styledMs: summary(samples.map((s) => s.styled.ms)),
    eventMs: summary(events.map((e) => e.duration)),
  };
}

//: The effort chip saves through PATCH /api/chats/:id. With the save held for
//: 500 ms, any frame showing the old value is a snap-back.
async function scenarioEffort(cdp, intercept) {
  await click(cdp, await centerOf(cdp, ".composer-menu-button"));
  await sleep(200);
  const present = await evaluate(cdp, `Boolean(document.querySelector('select[aria-label="Select effort"]'))`);
  if (!present) {
    await park(cdp);
    return { skipped: "effort select not in the menu (agent mode?)" };
  }
  await intercept.set([{ method: "PATCH", path: /^\/api\/chats\/\d+$/, delayMs: 500 }]);
  const samples = [];
  try {
    for (let i = 0; i < REPEATS; i += 1) {
      const result = await evaluate(cdp, `(() => new Promise((done) => {
        const select = document.querySelector('select[aria-label="Select effort"]');
        const next = select.value === "low" ? "high" : "low";
        const start = performance.now();
        select.value = next;
        select.dispatchEvent(new Event("change", { bubbles: true }));
        let frames = 0, wrong = 0, firstRightFrame = null;
        const tick = () => {
          frames += 1;
          const current = document.querySelector('select[aria-label="Select effort"]');
          if (current.value === next) { if (firstRightFrame === null) firstRightFrame = frames; }
          else wrong += 1;
          if (performance.now() - start < 450) requestAnimationFrame(tick);
          else done({ frames, wrong, firstRightFrame });
        };
        requestAnimationFrame(tick);
      }))()`);
      samples.push(result);
      await sleep(700); // let the held save land before the next change
    }
  } finally {
    await intercept.set([]);
    await park(cdp);
    await evaluate(cdp, `document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }))`);
  }
  return {
    snapBackSamples: samples.filter((s) => s.wrong > 0).length,
    wrongFrames: summary(samples.map((s) => s.wrong)),
    firstRightFrame: summary(samples.map((s) => s.firstRightFrame)),
  };
}

//: Click a chat row while its thread is held for 300 ms.
async function scenarioOpenChat(cdp, intercept) {
  await intercept.set([{ method: "GET", path: /^\/api\/chats\/\d+$/, delayMs: 300 }]);
  const highlight = [];
  const content = [];
  const events = [];
  try {
    for (let i = 0; i < REPEATS; i += 1) {
      const index = 1 + (i % 6);
      const target = await evaluate(cdp, `(() => {
        const rows = [...document.querySelectorAll(".chat-item:not(.active)")];
        const row = rows[${index}];
        return row ? { id: row.dataset.chatId, title: row.querySelector(".chat-item-title").textContent.trim() } : null;
      })()`);
      if (!target) break;
      await evaluate(cdp, "window.__bench.reset()");
      const number = Number((target.title.match(/(\d+)/) || [])[1]);
      const wait = evaluate(cdp, `(() => {
        const row = () => document.querySelector('.chat-item[data-chat-id="${target.id}"]');
        const shows = () => [...document.querySelectorAll(".neo-chat-message")].some((m) => m.textContent.includes("in chat ${number}?"));
        return Promise.all([window.__bench.until(() => row()?.classList.contains("active"), 3000, "click"), window.__bench.until(shows, 3000, "click")]);
      })()`);
      await click(cdp, await centerOf(cdp, `.chat-item[data-chat-id="${target.id}"] .chat-item-title`));
      const [lit, shown] = await wait;
      highlight.push(lit.ms);
      content.push(shown.ms);
      events.push(...(await collect(cdp)).events.filter((e) => e.name === "click"));
      await sleep(250);
    }
  } finally {
    await intercept.set([]);
    await park(cdp);
  }
  return { highlightMs: summary(highlight), contentMs: summary(content), clickEventMs: summary(events.map((e) => e.duration)) };
}

async function scenarioTyping(cdp) {
  await click(cdp, await centerOf(cdp, ".chat-input-form textarea"));
  await evaluate(cdp, "window.__bench.reset()");
  const text = "the quick brown fox jumps over the lazy dog again and again";
  for (let i = 0; i < Math.max(1, Math.round(REPEATS / 5)); i += 1) await typeText(cdp, text, 45);
  const seen = await collect(cdp);
  // Clear the draft so later scenarios start from the same composer.
  await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key: "a", modifiers: 4, commands: ["selectAll"] });
  await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Backspace", windowsVirtualKeyCode: 8 });
  await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Backspace", windowsVirtualKeyCode: 8 });
  await park(cdp);
  const keyEvents = seen.events.filter((e) => ["keydown", "keypress", "keyup", "beforeinput", "input"].includes(e.name));
  const keystrokes = text.length * Math.max(1, Math.round(REPEATS / 5));
  return {
    //: Event Timing only reports events of 16 ms or more, so this is the
    //: distribution of the slow ones plus how many keystrokes produced one.
    keystrokes,
    slowKeyEntries: keyEvents.length,
    keyEventMs: summary(keyEvents.map((e) => e.duration)),
    longTasks: seen.longTasks.length,
    longTaskMaxMs: quantile(seen.longTasks.map((t) => t.duration), 1),
    longFrames: seen.frames.filter((f) => f.duration > 50).length,
  };
}

//: Entrance animations played by switching chats, and by a workspace round trip.
async function scenarioArrivals(cdp) {
  const perSwitch = [];
  for (let i = 0; i < Math.min(REPEATS, 6); i += 1) {
    const id = await evaluate(cdp, `document.querySelectorAll(".chat-item:not(.active)")[${i % 4}]?.dataset.chatId`);
    await evaluate(cdp, "window.__bench.reset()");
    await click(cdp, await centerOf(cdp, `.chat-item[data-chat-id="${id}"] .chat-item-title`));
    await evaluate(cdp, `window.__bench.until(() => document.querySelector('.chat-item[data-chat-id="${id}"]')?.classList.contains("active"), 3000)`);
    await sleep(500);
    perSwitch.push((await collect(cdp)).arrivals);
  }
  await evaluate(cdp, "window.__bench.reset()");
  const notes = await centerOf(cdp, ".system-nav button", 0);
  const roundTrip = await evaluate(cdp, `(() => [...document.querySelectorAll(".system-nav button")].findIndex((b) => /Notes/.test(b.textContent)))()`);
  let workspace = null;
  if (roundTrip >= 0 && notes) {
    await click(cdp, await centerOf(cdp, ".system-nav button", roundTrip));
    await evaluate(cdp, `window.__bench.until(() => document.querySelector(".nw"), 4000)`);
    await sleep(300);
    await evaluate(cdp, "window.__bench.reset()");
    await click(cdp, await centerOf(cdp, ".chat-item.active .chat-item-title"));
    await evaluate(cdp, `window.__bench.until(() => document.querySelector(".neo-chat-message"), 4000)`);
    await sleep(500);
    workspace = (await collect(cdp)).arrivals;
  }
  return { perChatSwitch: summary(perSwitch), workspaceRoundTrip: workspace };
}

//: First open of a lazy panel: did the fallback show, and how long until the panel.
async function scenarioPanel(cdp) {
  const index = await evaluate(cdp, `[...document.querySelectorAll(".system-nav button")].findIndex((b) => /Notes/.test(b.textContent))`);
  if (index < 0) return { skipped: "Notes is hidden in this profile" };
  await evaluate(cdp, "window.__bench.reset()");
  const wait = evaluate(cdp, `window.__bench.until(() => document.querySelector(".nw"), 5000, "click")`);
  await click(cdp, await centerOf(cdp, ".system-nav button", index));
  const opened = await wait;
  const seen = await collect(cdp);
  await click(cdp, await centerOf(cdp, ".chat-item.active .chat-item-title"));
  await evaluate(cdp, `window.__bench.until(() => document.querySelector(".neo-chat-message"), 4000)`);
  await park(cdp);
  return { fallbackShown: seen.fallbacks > 0, openMs: opened.ms ? +opened.ms.toFixed(1) : null };
}

//: Text of a sidebar row's title without its pin glyph or status badge.
const ROW_TITLE = (id) => `(() => {
  const el = document.querySelector('.chat-item[data-chat-id="${id}"] .chat-item-title');
  return el ? [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join("").trim() : null;
})()`;

//: Three chat rows clicked 40 ms apart with their threads held for different
//: times. In either order of answers the last click must end on screen and
//: highlighted, with no error, and the superseded fetches cancelled.
async function scenarioOpenRace(cdp, intercept) {
  const runs = [];
  const rounds = Math.max(1, Math.round(REPEATS / 5));
  try {
    for (const delays of [[900, 500, 150], [150, 500, 900]]) {
      for (let round = 0; round < rounds; round += 1) {
        const rows = await evaluate(cdp, `[...document.querySelectorAll(".chat-item:not(.active)")]
          .slice(${2 + round * 3}, ${5 + round * 3})
          .map((row) => ({ id: row.dataset.chatId, title: row.querySelector(".chat-item-title").textContent.trim() }))`);
        if (rows.length < 3) break;
        await intercept.set(rows.map((row, k) => ({ method: "GET", path: new RegExp(`^/api/chats/${row.id}$`), delayMs: delays[k] })));
        const since = intercept.log.length;
        await evaluate(cdp, "window.__bench.reset()");
        const last = rows[2];
        const number = Number((last.title.match(/(\d+)/) || [])[1]);
        await click(cdp, await centerOf(cdp, `.chat-item[data-chat-id="${rows[0].id}"] .chat-item-title`));
        await sleep(40);
        await click(cdp, await centerOf(cdp, `.chat-item[data-chat-id="${rows[1].id}"] .chat-item-title`));
        await sleep(40);
        const highlight = evaluate(cdp, `window.__bench.until(() => document.querySelector('.chat-item[data-chat-id="${last.id}"]')?.classList.contains("active"), 3000, "click")`);
        await click(cdp, await centerOf(cdp, `.chat-item[data-chat-id="${last.id}"] .chat-item-title`));
        const lit = await highlight;
        await sleep(Math.max(...delays) + 800);
        const final = await evaluate(cdp, `(() => ({
          active: document.querySelector(".chat-item.active")?.dataset.chatId || null,
          shows: [...document.querySelectorAll(".neo-chat-message")].some((m) => m.textContent.includes("in chat ${number}?")),
          error: document.querySelector(".neo-error")?.textContent || null,
        }))()`);
        const fetched = intercept.log.slice(since).filter((e) => e.method === "GET" && /^\/api\/chats\/\d+$/.test(e.path));
        runs.push({
          delays: delays.join("/"),
          endedOnLast: final.active === last.id && final.shows,
          error: final.error,
          highlightMs: lit.ms,
          canceled: fetched.filter((e) => e.outcome === "canceled").length,
        });
      }
    }
  } finally {
    await intercept.set([]);
    await park(cdp);
  }
  return {
    runs: runs.length,
    endedOnLastClick: runs.filter((r) => r.endedOnLast).length,
    errors: runs.filter((r) => r.error).length,
    lastClickHighlightMs: summary(runs.map((r) => r.highlightMs)),
    canceledFetches: summary(runs.map((r) => r.canceled)),
    detail: runs,
  };
}

//: The effort chip changed three times inside one held save, then a save that
//: fails: never two PATCHes for the chat at once, the screen and the server
//: ending on the last choice, and a failure putting back only what it asked
//: for, to the last stored value.
async function scenarioSettingsRace(cdp, intercept) {
  await click(cdp, await centerOf(cdp, ".composer-menu-button"));
  await sleep(200);
  const chatId = await evaluate(cdp, `document.querySelector(".chat-item.active")?.dataset.chatId || null`);
  const present = await evaluate(cdp, `Boolean(document.querySelector('select[aria-label="Select effort"]'))`);
  if (!present || !chatId) { await park(cdp); return { skipped: "effort select or active chat missing" }; }
  const path = new RegExp(`^/api/chats/${chatId}$`);
  const pickAll = (values, watchMs) => evaluate(cdp, `(async () => {
    const select = () => document.querySelector('select[aria-label="Select effort"]');
    let expected = null, wrong = 0;
    const watch = () => { if (expected !== null && select().value !== expected) wrong += 1; };
    for (const value of ${JSON.stringify(values)}) {
      const s = select(); s.value = value; s.dispatchEvent(new Event("change", { bubbles: true }));
      expected = value;
      await new Promise((r) => requestAnimationFrame(r)); watch();
      await new Promise((r) => setTimeout(r, 50));
    }
    const until = performance.now() + ${watchMs};
    while (performance.now() < until) { await new Promise((r) => requestAnimationFrame(r)); watch(); }
    return { wrong, last: select().value };
  })()`, 20_000);
  const serverEffort = () => evaluate(cdp, `fetch("/api/chats/${chatId}").then((r) => r.json()).then((t) => t.chat.effort)`);
  try {
    // 1. Three changes inside one held save.
    await intercept.set([{ method: "PATCH", path, delayMs: 300 }]);
    let since = intercept.log.length;
    const initial = await evaluate(cdp, `document.querySelector('select[aria-label="Select effort"]').value`);
    const flip = (v) => (v === "low" ? "high" : "low");
    const three = [flip(initial), initial, flip(initial)];
    const first = await pickAll(three, 1500);
    const firstServer = await serverEffort();
    const firstOverlap = intercept.maxOverlap("PATCH", path, since);
    const firstPatches = intercept.log.slice(since).filter((e) => e.method === "PATCH" && path.test(e.path)).length;
    // 2. A stored change, then one whose save fails.
    await intercept.set([
      { method: "PATCH", path, delayMs: 300, nth: 1 },
      { method: "PATCH", path, fail: true, nth: 2 },
    ]);
    since = intercept.log.length;
    const start = await evaluate(cdp, `document.querySelector('select[aria-label="Select effort"]').value`);
    const second = await pickAll([flip(start), start], 1500);
    const secondServer = await serverEffort();
    return {
      quickChanges: {
        snapBackFrames: first.wrong,
        uiEndsOnLast: first.last === three[2],
        serverEndsOnLast: firstServer === three[2],
        patches: firstPatches,
        maxPatchesInFlight: firstOverlap,
      },
      failedSave: {
        // The failed change goes back to what the server last stored.
        uiShowsStored: second.last === secondServer,
        stored: secondServer,
        expectedStored: flip(start),
        maxPatchesInFlight: intercept.maxOverlap("PATCH", path, since),
        message: await evaluate(cdp, `document.querySelector(".neo-error, .agent-mode-message")?.textContent || null`),
      },
    };
  } finally {
    await intercept.set([]);
    await park(cdp);
    await evaluate(cdp, `document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }))`);
  }
}

async function rowMenu(cdp, chatId, label) {
  const row = `.chat-item[data-chat-id="${chatId}"]`;
  const title = await centerOf(cdp, `${row} .chat-item-title`);
  await mouse(cdp, "mouseMoved", title.x, title.y);
  await sleep(60);
  await click(cdp, await centerOf(cdp, `${row} .row-actions-trigger`));
  await evaluate(cdp, `window.__bench.until(() => document.querySelector(".row-actions-menu:not([hidden])"), 2000)`);
  const index = await evaluate(cdp, `[...document.querySelectorAll(".row-actions-menu:not([hidden]) button")].findIndex((b) => b.textContent.trim() === ${JSON.stringify(label)})`);
  if (index < 0) throw new Error(`row menu has no ${label}`);
  await click(cdp, await centerOf(cdp, ".row-actions-menu:not([hidden]) button", index));
}

async function renameRow(cdp, chatId, title) {
  await rowMenu(cdp, chatId, "Rename");
  await evaluate(cdp, `window.__bench.until(() => document.querySelector('.chat-item[data-chat-id="${chatId}"] .chat-rename-input'), 2000)`);
  await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key: "a", modifiers: 4, commands: ["selectAll"] });
  await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: "a", modifiers: 4 });
  await typeText(cdp, title, 4);
  await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, text: "\r" });
  await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 });
}

//: A row renamed twice inside one held save, then pinned, while the sidebar's
//: own reads are held back: the row never shows a title older than the last one
//: asked for, never two PATCHes at once, and the server ends on the last title,
//: pinned.
async function scenarioSidebarRace(cdp, intercept) {
  const chatId = await evaluate(cdp, `document.querySelectorAll(".chat-item:not(.active)")[3]?.dataset.chatId || null`);
  if (!chatId) return { skipped: "no row to edit" };
  const path = new RegExp(`^/api/chats/${chatId}$`);
  try {
    await intercept.set([
      { method: "PATCH", path, delayMs: 300 },
      { method: "GET", path: /^\/api\/sidebar$/, delayMs: 800 },
    ]);
    const since = intercept.log.length;
    await renameRow(cdp, chatId, "Renamed one");
    await sleep(60);
    await renameRow(cdp, chatId, "Renamed two");
    const sampler = evaluate(cdp, `new Promise((done) => {
      const start = performance.now();
      let stale = 0, frames = 0;
      const tick = () => {
        frames += 1;
        const title = ${ROW_TITLE(chatId)};
        if (title !== null && title !== "Renamed two") stale += 1;
        if (performance.now() - start < 3000) requestAnimationFrame(tick); else done({ stale, frames });
      };
      requestAnimationFrame(tick);
    })`, 10_000);
    await sleep(80);
    await rowMenu(cdp, chatId, "Pin chat");
    const seen = await sampler;
    await sleep(1500);
    const server = await evaluate(cdp, `fetch("/api/chats/${chatId}").then((r) => r.json()).then((t) => ({ title: t.chat.title, pinned: t.chat.pinned }))`);
    return {
      framesShowingOlderTitle: seen.stale,
      sampledFrames: seen.frames,
      serverTitle: server.title,
      serverPinned: server.pinned,
      rowTitle: await evaluate(cdp, ROW_TITLE(chatId)),
      maxPatchesInFlight: intercept.maxOverlap("PATCH", path, since),
    };
  } finally {
    await intercept.set([]);
    await park(cdp);
  }
}

const WATCHED = ["NeoApp", "Sidebar", "SidebarChatRow", "ChatComposer", "ChatTranscript", "ChatMessage", "PendingAssistantMessage", "DictationMeter"];
const pickWatched = (renders) => Object.fromEntries(WATCHED.map((name) => [name, renders[name] || 0]));

//: Typing into the composer, in a development build: which components a
//: keystroke re-renders. Only the composer (and NeoApp, which owns the draft)
//: should; the sidebar and the transcript should not render at all.
async function scenarioCommitsTyping(cdp) {
  const ready = await evaluate(cdp, "Boolean(window.__commits)");
  if (!ready) return { skipped: "commit probe not installed (set BENCH_COMMITS=1, development build)" };
  await click(cdp, await centerOf(cdp, ".chat-input-form textarea"));
  await sleep(300);
  await evaluate(cdp, "window.__commits.reset()");
  const text = "commit probe typing";
  await typeText(cdp, text, 60);
  await sleep(300);
  const seen = await evaluate(cdp, "({ commits: window.__commits.commits, renders: { ...window.__commits.renders } })");
  await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key: "a", modifiers: 4, commands: ["selectAll"] });
  await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Backspace", windowsVirtualKeyCode: 8 });
  await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Backspace", windowsVirtualKeyCode: 8 });
  await park(cdp);
  const top = Object.entries(seen.renders).sort((a, b) => b[1] - a[1]).slice(0, 40);
  return { keystrokes: text.length, commits: seen.commits, renders: pickWatched(seen.renders), allTop: Object.fromEntries(top) };
}

//: A real streamed reply, in a development build: how often the sidebar and
//: the composer render while tokens arrive (only status changes should).
async function scenarioCommitsStream(cdp) {
  const ready = await evaluate(cdp, "Boolean(window.__commits)");
  if (!ready) return { skipped: "commit probe not installed (set BENCH_COMMITS=1, development build)" };
  await click(cdp, await centerOf(cdp, ".chat-input-form textarea"));
  await typeText(cdp, "Count from one to forty in words, separated by commas.", 5);
  await evaluate(cdp, "window.__commits.reset()");
  const done = evaluate(cdp, `new Promise((resolve) => {
    const start = performance.now();
    let sawPending = false;
    const tick = () => {
      const pending = Boolean(document.querySelector(".pending-message-bubble"));
      if (pending) sawPending = true;
      if ((sawPending && !pending) || performance.now() - start > 150000) return resolve({ finished: sawPending && !pending, ms: performance.now() - start });
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  })`, 160_000);
  await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, text: "\r" });
  await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 });
  const result = await done;
  const seen = await evaluate(cdp, "({ commits: window.__commits.commits, renders: { ...window.__commits.renders } })");
  await park(cdp);
  return { finished: result.finished, streamMs: Math.round(result.ms), commits: seen.commits, renders: pickWatched(seen.renders) };
}

//: Panel code ahead of the open. A first open straight after load still shows
//: the fallback (nothing has been fetched yet); after a few idle seconds the
//: sidebar's destinations are already here; a brief hover is enough for the
//: rest; and a device asking for economy fetches nothing in the background.
async function scenarioPreload(cdp) {
  const panelRequests = [];
  cdp.on("Network.requestWillBeSent", ({ request }) => {
    const match = request.url.match(/\/assets\/([A-Za-z]+)-[\w-]+\.js$/);
    if (match && match[1] !== "index") panelRequests.push(match[1]);
  });
  const reload = async () => {
    await cdp.send("Page.navigate", { url: ORIGIN });
    for (let attempt = 0; attempt < 120; attempt += 1) {
      await sleep(100);
      try {
        if (await evaluate(cdp, `document.querySelectorAll(".chat-item").length >= 10`)) return;
      } catch { /* navigating */ }
    }
    throw new Error("the app never came back after reload");
  };
  const open = async (label, root) => {
    const index = await evaluate(cdp, `[...document.querySelectorAll(".system-nav button")].findIndex((b) => b.textContent.trim() === ${JSON.stringify(label)})`);
    if (index < 0) return { skipped: `${label} is hidden in this profile` };
    await evaluate(cdp, "window.__bench.reset()");
    const point = await centerOf(cdp, ".system-nav button", index);
    const wait = evaluate(cdp, `window.__bench.until(() => document.querySelector(${JSON.stringify(root)}), 6000, "click")`);
    await click(cdp, point);
    const opened = await wait;
    const fallbackShown = (await collect(cdp)).fallbacks > 0;
    return { fallbackShown, openMs: opened.ms };
  };
  await reload();
  const cold = await open("Notes", ".nw");
  await reload();
  const before = panelRequests.length;
  await sleep(4500);
  const backgroundFetched = panelRequests.slice(before);
  const afterIdle = await open("Notes", ".nw");
  await reload();
  const gallery = await centerOf(cdp, ".system-nav button", await evaluate(cdp, `[...document.querySelectorAll(".system-nav button")].findIndex((b) => b.textContent.trim() === "Gallery")`));
  await mouse(cdp, "mouseMoved", gallery.x, gallery.y);
  await sleep(250);
  const afterHover = await open("Gallery", ".gal");
  // A device asking for economy: no background fetches at all.
  const stub = await cdp.send("Page.addScriptToEvaluateOnNewDocument", {
    source: `Object.defineProperty(navigator, "connection", { configurable: true, get: () => ({ saveData: true, effectiveType: "4g" }) });`,
  });
  await reload();
  const constrainedStart = panelRequests.length;
  await park(cdp);
  await sleep(4500);
  const constrainedFetched = panelRequests.slice(constrainedStart);
  await cdp.send("Page.removeScriptToEvaluateOnNewDocument", { identifier: stub.identifier });
  await reload();
  return {
    coldFirstOpen: cold,
    backgroundFetched,
    afterIdle,
    afterHover,
    backgroundFetchedWithSaveData: constrainedFetched,
  };
}

//: Every class token rendered on the main screens, checked against Tailwind
//: utilities that are no longer generated, with a screenshot of each screen
//: (BENCH_SHOTS names a directory) for a before/after comparison.
const REMOVED_UTILITIES = ["container", "visible", "invisible", "collapse", "static", "fixed", "absolute", "relative",
  "block", "inline", "flex", "table", "grid", "contents", "hidden", "shrink", "grow", "transform", "resize",
  "rounded", "lowercase", "italic", "shadow", "outline", "ring", "blur", "filter", "backdrop-filter", "transition"];
async function scenarioVisual(cdp) {
  const tokens = new Map();
  const shots = process.env.BENCH_SHOTS || null;
  const capture = async (name) => {
    await sleep(500);
    const found = await evaluate(cdp, `[...new Set([...document.querySelectorAll("[class]")].flatMap((el) => [...el.classList]))]`);
    for (const token of found) if (!tokens.has(token)) tokens.set(token, name);
    if (shots) {
      const { mkdir } = await import("node:fs/promises");
      await mkdir(shots, { recursive: true });
      const shot = await cdp.send("Page.captureScreenshot", { format: "png" });
      await writeFile(join(shots, `${LABEL}-${name}.png`), Buffer.from(shot.data, "base64"));
    }
  };
  const openNav = async (label, root) => {
    const index = await evaluate(cdp, `[...document.querySelectorAll(".system-nav button")].findIndex((b) => b.textContent.trim() === ${JSON.stringify(label)})`);
    if (index < 0) return false;
    await click(cdp, await centerOf(cdp, ".system-nav button", index));
    await evaluate(cdp, `window.__bench.until(() => document.querySelector(${JSON.stringify(root)}), 6000)`);
    return true;
  };
  const backToChat = async () => {
    await click(cdp, await centerOf(cdp, ".chat-item .chat-item-title"));
    await evaluate(cdp, `window.__bench.until(() => document.querySelector(".neo-chat-message"), 6000)`);
  };
  await park(cdp);
  await capture("chat");
  await click(cdp, await centerOf(cdp, ".composer-menu-button"));
  await capture("composer-menu");
  await evaluate(cdp, `document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }))`);
  await click(cdp, await centerOf(cdp, ".sidebar-settings"));
  await evaluate(cdp, `window.__bench.until(() => document.querySelector(".settings-dialog"), 6000)`);
  await capture("settings");
  await evaluate(cdp, `document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }))`);
  await sleep(300);
  if (await openNav("Notes", ".nw")) { await capture("notes"); await backToChat(); }
  if (await openNav("Gallery", ".gal")) { await capture("gallery"); await backToChat(); }
  const hits = [...tokens.entries()].filter(([token]) => REMOVED_UTILITIES.includes(token));
  return { classTokens: tokens.size, removedUtilityTokensInUse: hits };
}

//: New chat pressed while a chat-open is still on its way: the new chat must
//: win, the slow thread must never appear, and no error is shown.
async function scenarioNewChatDuringOpen(cdp, intercept) {
  const runs = [];
  try {
    for (let round = 0; round < Math.max(1, Math.round(REPEATS / 5)); round += 1) {
      const row = await evaluate(cdp, `(() => { const r = document.querySelectorAll(".chat-item:not(.active)")[${4 + round}]; return r ? { id: r.dataset.chatId, title: r.querySelector(".chat-item-title").textContent.trim() } : null; })()`);
      if (!row) break;
      const number = Number((row.title.match(/(\d+)/) || [])[1]);
      await intercept.set([{ method: "GET", path: new RegExp(`^/api/chats/${row.id}$`), delayMs: 900 }]);
      await click(cdp, await centerOf(cdp, `.chat-item[data-chat-id="${row.id}"] .chat-item-title`));
      await sleep(80);
      await click(cdp, await centerOf(cdp, ".sidebar-primary-action .neo-button"));
      await sleep(1500);
      const final = await evaluate(cdp, `(() => ({
        slowThreadShown: [...document.querySelectorAll(".neo-chat-message")].some((m) => m.textContent.includes("in chat ${number}?")),
        slowRowActive: Boolean(document.querySelector('.chat-item.active[data-chat-id="${row.id}"]')),
        error: document.querySelector(".neo-error")?.textContent || null,
      }))()`);
      runs.push(final);
      await intercept.set([]);
      // Back to a seeded chat for the next round.
      await click(cdp, await centerOf(cdp, ".chat-item .chat-item-title"));
      await sleep(500);
    }
  } finally {
    await intercept.set([]);
    await park(cdp);
  }
  return {
    runs: runs.length,
    newChatWon: runs.filter((r) => !r.slowThreadShown && !r.slowRowActive).length,
    errors: runs.filter((r) => r.error).length,
    detail: runs,
  };
}

//: A chat's turn ends while the person has already clicked another one: the
//: turn-end reload of the first chat is held, and the click on the second lands
//: during it. The view must end on the second chat. Needs the default model.
async function scenarioNavDuringRefresh(cdp, intercept) {
  // Start from a seeded chat, so both ends of the race are rows in the sidebar.
  const start = await evaluate(cdp, `document.querySelectorAll(".chat-item")[1]?.dataset.chatId || null`);
  if (start) {
    await click(cdp, await centerOf(cdp, `.chat-item[data-chat-id="${start}"] .chat-item-title`));
    await evaluate(cdp, `window.__bench.until(() => document.querySelector('.chat-item.active[data-chat-id="${start}"]') && document.querySelector(".neo-chat-message"), 5000)`);
    await sleep(400);
  }
  const from = await evaluate(cdp, `document.querySelector(".chat-item.active")?.dataset.chatId || null`);
  const to = await evaluate(cdp, `(() => { const r = document.querySelectorAll(".chat-item:not(.active)")[2]; return r ? { id: r.dataset.chatId, title: r.querySelector(".chat-item-title").textContent.trim() } : null; })()`);
  if (!from || !to) return { skipped: "need an open seeded chat and another row" };
  const number = Number((to.title.match(/(\d+)/) || [])[1]);
  try {
    await click(cdp, await centerOf(cdp, ".chat-input-form textarea"));
    await typeText(cdp, "Reply with the single word ok.", 8);
    await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, text: "\r" });
    await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 });
    // Hold the next read of this chat: that is the turn-end reload.
    await sleep(300);
    const since = intercept.log.length;
    await intercept.set([{ method: "GET", path: new RegExp(`^/api/chats/${from}$`), delayMs: 1500 }]);
    let held = false;
    for (let waited = 0; waited < 150_000 && !held; waited += 100) {
      await sleep(100);
      held = intercept.log.slice(since).some((e) => e.method === "GET" && e.path === `/api/chats/${from}`);
    }
    if (!held) return { skipped: "the turn never ended" };
    await click(cdp, await centerOf(cdp, `.chat-item[data-chat-id="${to.id}"] .chat-item-title`));
    await sleep(2500);
    const final = await evaluate(cdp, `(() => ({
      active: document.querySelector(".chat-item.active")?.dataset.chatId || null,
      shows: [...document.querySelectorAll(".neo-chat-message")].some((m) => m.textContent.includes("in chat ${number}?")),
      error: document.querySelector(".neo-error")?.textContent || null,
    }))()`);
    return { endedOnClickedChat: final.active === to.id && final.shows, error: final.error };
  } finally {
    await intercept.set([]);
    await park(cdp);
  }
}

//: Long tasks and long animation frames across a busy minute of use: the
//: pointer sweeping the sidebar, a panel opened and closed, a burst of typing.
async function scenarioLongTasks(cdp) {
  await evaluate(cdp, "window.__bench.reset()");
  const rows = await evaluate(cdp, `[...document.querySelectorAll(".chat-item .chat-item-title, .system-nav button, .composer-tool, .chat-mode-switch button")].slice(0, 40).map((el) => { const r = el.getBoundingClientRect(); return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) }; })`);
  for (let pass = 0; pass < 3; pass += 1) {
    for (const point of rows) {
      await mouse(cdp, "mouseMoved", point.x, point.y);
      await sleep(16);
    }
  }
  const notes = await evaluate(cdp, `[...document.querySelectorAll(".system-nav button")].findIndex((b) => b.textContent.trim() === "Notes")`);
  if (notes >= 0) {
    await click(cdp, await centerOf(cdp, ".system-nav button", notes));
    await evaluate(cdp, `window.__bench.until(() => document.querySelector(".nw"), 6000)`);
    await sleep(300);
    await click(cdp, await centerOf(cdp, ".chat-item .chat-item-title"));
    await evaluate(cdp, `window.__bench.until(() => document.querySelector(".neo-chat-message"), 6000)`);
  }
  await click(cdp, await centerOf(cdp, ".chat-input-form textarea"));
  await typeText(cdp, "a burst of typing to finish the session", 30);
  await sleep(500);
  const seen = await collect(cdp);
  await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key: "a", modifiers: 4, commands: ["selectAll"] });
  await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Backspace", windowsVirtualKeyCode: 8 });
  await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Backspace", windowsVirtualKeyCode: 8 });
  await park(cdp);
  const long = seen.frames.filter((f) => f.duration > 50);
  return {
    longTasks: seen.longTasks.length,
    longTaskMaxMs: quantile(seen.longTasks.map((t) => t.duration), 1),
    longAnimationFrames: long.length,
    longAnimationFrameMaxMs: quantile(long.map((f) => f.duration), 1),
    slowInteractions: seen.events.filter((e) => e.interactionId && e.duration > 100).length,
    interactionMs: summary(seen.events.filter((e) => e.interactionId).map((e) => e.duration)),
  };
}

//: One real send, answered by the profile's default model: how many rows play
//: an entrance from the send to the end of its turn (the bubble and its pending
//: reply should be the only ones), and whether the pending reply and the saved
//: one it becomes ever share a frame.
async function scenarioSend(cdp) {
  await click(cdp, await centerOf(cdp, ".chat-input-form textarea"));
  await typeText(cdp, "Reply with the single word ok.", 10);
  await evaluate(cdp, "window.__bench.reset()");
  const watch = evaluate(cdp, `new Promise((done) => {
    const saved = () => document.querySelectorAll(".neo-chat-message.assistant:not(.thinking)").length;
    const before = saved();
    const start = performance.now();
    let both = 0;
    let sawPending = false;
    const tick = () => {
      const pending = Boolean(document.querySelector(".pending-message-bubble"));
      if (pending) sawPending = true;
      const replied = saved() > before;
      if (pending && replied) both += 1;
      const finished = sawPending && replied && !pending;
      if (finished || performance.now() - start > 150000) {
        return done({ finished, both, ms: performance.now() - start, failed: Boolean(document.querySelector(".neo-chat-message.user .chat-message-status")) });
      }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  })`, 160_000);
  await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, text: "\r" });
  await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 });
  const result = await watch;
  await sleep(600);
  const seen = await collect(cdp);
  await park(cdp);
  return {
    finished: result.finished,
    sendFailed: result.failed,
    entranceAnimations: seen.arrivals,
    framesWithBothReplies: result.both,
    turnMs: Math.round(result.ms),
  };
}

/* ------------------------------------------------------------ driver -- */

//: Where the disposable profile's credentials live while a run is going. Written
//: the moment the profile exists and removed once it is deleted, so a run that
//: dies (a crashed browser, a killed process) is cleaned up by the next one.
const STATE = process.env.BENCH_STATE || join(tmpdir(), "neo-interaction-profile.json");

//: Deleted from Node, not from the page: it has to work when the browser is gone.
async function deleteProfile({ origin, profileId, password }) {
  const response = await fetch(new URL(`api/account-profiles/${profileId}`, origin), {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
  if (response.status !== 204 && response.status !== 401) {
    throw new Error(`deleting ${profileId} answered ${response.status}`);
  }
  // 401 means the profile is already gone (or the password no longer matches).
  return response.status === 204;
}

async function cleanupStale() {
  let stale;
  try { stale = JSON.parse(await readFile(STATE, "utf8")); } catch { return; }
  try {
    const deleted = await deleteProfile(stale);
    process.stderr.write(`Removed benchmark profile left by an earlier run (${stale.profileId}): ${deleted}\n`);
  } catch (error) {
    process.stderr.write(`Could not remove the earlier run's profile ${stale.profileId}: ${error.message}\n`);
    throw error;
  }
  await rm(STATE, { force: true });
}

async function main() {
  await cleanupStale();
  const profileDir = await mkdtemp(join(tmpdir(), "neo-interaction-"));
  const chrome = spawn(CHROME, [
    `--remote-debugging-port=${PORT}`,
    `--window-size=${WINDOW}`,
    `--user-data-dir=${profileDir}`,
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "about:blank",
  ], { stdio: "ignore" });

  const username = `bench${Date.now().toString(36)}`;
  const password = `bench-${randomUUID()}`;
  let profile = null;
  let cdp = null;
  let cleaned = false;
  const report = { label: LABEL, origin: ORIGIN, cache: CACHE, repeats: REPEATS, results: {} };

  const cleanup = async () => {
    if (cleaned) return;
    cleaned = true;
    if (profile) {
      try {
        report.profileDeleted = await deleteProfile(profile);
        await rm(STATE, { force: true });
      } catch (error) {
        report.profileDeleted = false;
        process.stderr.write(`Could not delete benchmark profile ${profile.profileId} (kept in ${STATE}): ${error.message}\n`);
      }
    }
    try { cdp?.close(); } catch { /* already closed */ }
    chrome.kill();
    await sleep(300);
    await rm(profileDir, { recursive: true, force: true });
    if (process.env.BENCH_OUTPUT) await writeFile(process.env.BENCH_OUTPUT, JSON.stringify(report, null, 2));
  };
  for (const signal of ["SIGINT", "SIGTERM"]) {
    process.once(signal, () => { cleanup().finally(() => process.exit(130)); });
  }

  try {
    cdp = connect(await pageSocket());
    await cdp.ready;
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Network.enable");
    if (CACHE === "cold") await cdp.send("Network.setCacheDisabled", { cacheDisabled: true });
    if (MOBILE) {
      await cdp.send("Emulation.setDeviceMetricsOverride", { width: 412, height: 915, deviceScaleFactor: 2.625, mobile: true });
      await cdp.send("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 5 });
      await cdp.send("Network.emulateNetworkConditions", { offline: false, latency: 150, downloadThroughput: 200_000, uploadThroughput: 93_750 });
    }
    await cdp.send("Page.addScriptToEvaluateOnNewDocument", { source: INSTRUMENT });
    if (process.env.BENCH_COMMITS) await cdp.send("Page.addScriptToEvaluateOnNewDocument", { source: COMMIT_PROBE });
    report.browser = (await cdp.send("Browser.getVersion")).product;
    const intercept = createInterceptor(cdp);

    await cdp.send("Page.navigate", { url: ORIGIN });
    // A cold dev server can take seconds to answer the first navigation; the
    // profile request needs the app's origin, not the blank page before it.
    let atOrigin = false;
    for (let attempt = 0; attempt < 120 && !atOrigin; attempt += 1) {
      await sleep(250);
      try { atOrigin = await evaluate(cdp, `location.href.startsWith(${JSON.stringify(ORIGIN)}) && document.readyState !== "loading"`); }
      catch { /* navigating */ }
    }
    if (!atOrigin) throw new Error(`the page never reached ${ORIGIN}`);
    const created = await evaluate(cdp, `fetch("/api/account-profiles", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: ${JSON.stringify(username)}, password: ${JSON.stringify(password)} }),
    }).then((r) => r.json())`);
    if (!created?.profile?.id) throw new Error(`could not create the benchmark profile: ${JSON.stringify(created)}`);
    profile = { origin: ORIGIN, profileId: created.profile.id, password, username };
    await writeFile(STATE, JSON.stringify(profile));
    const seeded = await run(PYTHON, [join(HERE, "seed_profile.py"), profile.profileId, "--chats", String(CHATS), "--long-chat-messages", String(LONG_CHAT_MESSAGES)], { cwd: REPO, maxBuffer: 16 * 1024 * 1024 });
    report.seed = JSON.parse(seeded.stdout).chat_ids.length;

    for (const rate of CPU_RATES) {
      await cdp.send("Emulation.setCPUThrottlingRate", { rate: 1 });
      await cdp.send("Page.navigate", { url: ORIGIN });
      let ready = false;
      for (let attempt = 0; attempt < 120 && !ready; attempt += 1) {
        await sleep(250);
        try { ready = await evaluate(cdp, `document.querySelectorAll(".chat-item").length >= 10 && Boolean(document.querySelector(".chat-input-form textarea"))`); }
        catch { /* navigating */ }
      }
      if (!ready) throw new Error("the app never showed the seeded chats");
      front();
      await cdp.send("Page.bringToFront");
      await sleep(800);
      await cdp.send("Emulation.setCPUThrottlingRate", { rate });
      const results = {};
      for (const name of SCENARIOS) {
        process.stderr.write(`  ${LABEL} cpu${rate}: ${name}\n`);
        const fn = {
          hover: () => scenarioHover(cdp),
          press: () => scenarioPress(cdp),
          mode: () => scenarioMode(cdp),
          effort: () => scenarioEffort(cdp, intercept),
          openChat: () => scenarioOpenChat(cdp, intercept),
          typing: () => scenarioTyping(cdp),
          arrivals: () => scenarioArrivals(cdp),
          panel: () => scenarioPanel(cdp),
          send: () => scenarioSend(cdp),
          openRace: () => scenarioOpenRace(cdp, intercept),
          settingsRace: () => scenarioSettingsRace(cdp, intercept),
          sidebarRace: () => scenarioSidebarRace(cdp, intercept),
          commitsTyping: () => scenarioCommitsTyping(cdp),
          commitsStream: () => scenarioCommitsStream(cdp),
          preload: () => scenarioPreload(cdp),
          visual: () => scenarioVisual(cdp),
          touchPress: () => scenarioTouchPress(cdp),
          tapPress: () => scenarioTapPress(cdp),
          newChatDuringOpen: () => scenarioNewChatDuringOpen(cdp, intercept),
          navDuringRefresh: () => scenarioNavDuringRefresh(cdp, intercept),
          longTasks: () => scenarioLongTasks(cdp),
        }[name];
        if (!fn) throw new Error(`unknown scenario ${name}`);
        results[name] = await fn();
      }
      report.results[`cpu${rate}`] = results;
      if (process.env.BENCH_OUTPUT) await writeFile(process.env.BENCH_OUTPUT, JSON.stringify(report, null, 2));
    }
  } finally {
    await cleanup();
  }
  console.log(JSON.stringify(report, null, 2));
}

await main();
