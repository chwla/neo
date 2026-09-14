/**
 * A frame-time bench for the chat background.
 *
 * Development only, and not part of the application: it is served by the Vite
 * dev server and never built. What it does that a DevTools recording cannot is
 * control scene variables -- same window size, same theme, same transcript
 * behind the glass -- while one layer at a time is switched off, so two runs
 * differ in exactly the thing being tested.
 *
 * It imports the real modules and the real stylesheet rather than a copy. The
 * glass surfaces are the production rules, the effects are the production
 * effects, and the engine is the production engine with its frame cap and its
 * device pixel ratio. A benchmark of a replica would be a benchmark of a
 * replica.
 *
 * Two different costs are measured, because they fail differently:
 *
 *   marks / diffusion   time inside `paint`, which is the effect's own work --
 *                       building paths, issuing canvas calls, reducing the frame
 *   frame interval      the gap between one animation frame and the next, which
 *                       is what the reader actually feels
 *
 * Cheap Canvas submissions with long intervals warrant a wider profile:
 * other main-thread work, rasterisation, compositing, display cadence and
 * browser scheduling can all contribute. Neither metric measures GPU execution
 * or proves which of those caused a delay.
 */

import "../src/index.css";
import { createEngine } from "../src/backgrounds/engine.js";
import { effectById } from "../src/backgrounds/effects.js";
import { intensityById } from "../src/backgrounds/index.js";
import { installDiagnostics } from "../src/backgrounds/diagnostics.js";

const params = new URLSearchParams(location.search);
const option = (name, fallback) => params.get(name) ?? fallback;
const off = (name) => params.get(name) === "0";

const background = option("bg", "gradient");
const intensity = option("intensity", "vivid");
const seconds = Number(option("seconds", 6));
const warmupMs = Number(option("warmup", 1500));

// Hold the randomized scene constant between before/after runs.
let seed = Number(option("seed", 1)) >>> 0;
Math.random = () => {
  seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
  return seed / 4294967296;
};

const bag = installDiagnostics();
bag.timing = true;
bag.layers.marks = !off("marks");
bag.layers.diffusion = !off("diffusion");

document.documentElement.dataset.theme = option("theme", "obsidian");
document.documentElement.dataset.chatBg = background;

/* Isolate glass surfaces by overriding production rules. A moving backdrop
   can invalidate their filters; the actual cost depends on the browser. The
   optimized composer strip is tint-only, so stripGlass is now a no-op there. */
const suppress = [];
if (off("sidebarGlass")) suppress.push(".neo-sidebar");
if (off("stripGlass")) suppress.push(".chat-input-wrap::before");
if (off("cardGlass")) suppress.push(".chat-input-shell");
if (off("wash")) suppress.push(".chat-bg-layer.has-wash::before");
if (suppress.length) {
  const style = document.createElement("style");
  style.textContent = suppress
    .map((selector) =>
      selector.includes("has-wash")
        ? `${selector} { display: none !important; }`
        : `[data-chat-bg] ${selector} { backdrop-filter: none !important; -webkit-backdrop-filter: none !important; }`,
    )
    .join("\n");
  document.head.append(style);
}

//: A transcript behind the glass, because an empty panel is not what the glass
//: is filtering in the application and an empty one composites differently.
const shell = document.getElementById("bench-shell");
shell.innerHTML = Array.from({ length: 24 }, (_, i) => `
  <article class="neo-chat-message ${i % 2 ? "assistant" : "user"}">
    <div class="message-stack">
      <span class="message-sender">${i % 2 ? "Neo" : "You"}</span>
      <div class="message-bubble"><div class="chat-content">
        Sample transcript line ${i + 1} sitting behind the glass so the surfaces
        above have real content to diffuse rather than flat colour.
      </div></div>
    </div>
  </article>`).join("");

document.getElementById("bench-sidebar").innerHTML = Array.from(
  { length: 18 },
  (_, i) => `<div style="padding:7px 12px;font-size:12px;opacity:.78">Conversation ${i + 1}</div>`,
).join("");

const layer = document.getElementById("bench-layer");
const marks = document.getElementById("bench-marks");
const diffusion = document.getElementById("bench-diffusion");
// Diagnostic isolation only: bypass Canvas2D's filter while retaining the
// downscale and amplification, to separate filter cost from bloom itself.
if (off("bloomBlur")) {
  const ctx = diffusion.getContext("2d");
  Object.defineProperty(ctx, "filter", { get: () => "none", set() {} });
}
const effect = effectById(background);
if (!effect) throw new Error(`no such background: ${background}`);
if (!(effect.bloom > 1)) layer.classList.remove("has-wash");

const engine = createEngine(marks, layer, {
  effect,
  intensity: intensityById(intensity),
  bloom: effect.bloom > 1 ? diffusion : null,
});

/* The interval between animation frames, sampled independently of the engine.
   Deliberately a separate rAF loop: the engine's own loop refuses refreshes it
   does not need, so timing from inside it would measure the cap rather than the
   machine. This one runs every refresh and records the gap. */
const intervals = [];
const longTasks = [];
let hadHiddenTime = document.hidden;
document.addEventListener("visibilitychange", () => { hadHiddenTime ||= document.hidden; });
const inputQueue = [];
const inputToRaf = [];
document.getElementById("bench-composer").addEventListener("keydown", (event) => {
  inputQueue.push(performance.now() - event.timeStamp);
  requestAnimationFrame(() => inputToRaf.push(performance.now() - event.timeStamp));
});
let longTaskObserver;
if (typeof PerformanceObserver === "function" && PerformanceObserver.supportedEntryTypes.includes("longtask")) {
  longTaskObserver = new PerformanceObserver((list) => longTasks.push(...list.getEntries().map((entry) => entry.duration)));
  longTaskObserver.observe({ type: "longtask" });
}
let previous = 0;
let raf = 0;
function sample(now) {
  if (previous) intervals.push(now - previous);
  previous = now;
  raf = requestAnimationFrame(sample);
}

const quantile = (sorted, q) => sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * q))];

function summarise(values) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const share = (limit) => (sorted.filter((v) => v > limit).length / sorted.length) * 100;
  return {
    n: sorted.length,
    mean: +(sorted.reduce((a, b) => a + b, 0) / sorted.length).toFixed(2),
    median: +quantile(sorted, 0.5).toFixed(2),
    p95: +quantile(sorted, 0.95).toFixed(2),
    max: +sorted[sorted.length - 1].toFixed(2),
    // Useful interval thresholds for 120/60/30Hz. These include clock jitter
    // and are not a count of dropped presented frames (8.3 < 1000 / 120).
    over8_3: +share(8.3).toFixed(1),
    over16_7: +share(16.7).toFixed(1),
    over33_3: +share(33.3).toFixed(1),
  };
}

window.__benchRun = (ms = seconds * 1000) =>
  new Promise((resolve) => {
    //: A moment of warm-up first, so first-frame layout, font work and the
    //: compositor's initial layer setup do not land in the sample.
    bag.reset();
    intervals.length = 0;
    previous = 0;
    setTimeout(() => {
      bag.reset();
      hadHiddenTime = document.hidden;
      intervals.length = 0;
      longTasks.length = 0;
      inputQueue.length = 0;
      inputToRaf.length = 0;
      previous = 0;
      setTimeout(() => {
        cancelAnimationFrame(raf);
        raf = 0;
        const paint = bag.report();
        resolve({
          config: {
            background, intensity,
            marks: bag.layers.marks, diffusion: bag.layers.diffusion,
            suppressed: suppress,
            dpr: window.devicePixelRatio,
            viewport: `${innerWidth}x${innerHeight}`,
            canvas: `${marks.width}x${marks.height}`,
            canvasPixels: marks.width * marks.height,
            warmupMs,
            visible: document.visibilityState,
            hadHiddenTime,
            seed: Number(option("seed", 1)),
            bloomBlur: !off("bloomBlur"),
          },
          interval: summarise(intervals),
          longTasks: summarise(longTasks),
          inputQueueMs: summarise(inputQueue),
          inputToRafMs: summarise(inputToRaf),
          engine: engine.getStats?.() || null,
          marksMs: paint.marks,
          diffusionMs: paint.diffusion,
          paintMs: paint.frame,
        });
      }, ms);
    }, warmupMs);
    if (!raf) raf = requestAnimationFrame(sample);
  });

raf = requestAnimationFrame(sample);
window.__benchReady = true;
window.__benchEngine = engine;
