/**
 * Drive the benchmark page in a real Chrome and collect the numbers.
 *
 * Runs a real browser window so measurements include normal browser rendering
 * and compositing. Frame cadence alone does not identify a bottleneck or prove
 * which GPU backend Chrome used.
 *
 * Talks the DevTools Protocol directly over the WebSocket that Node now ships,
 * so there is no browser-automation dependency to install for a diagnostic.
 */
import { spawn, execFileSync } from "node:child_process";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";

// Activating Chrome may select a different profile's window. Page.bringToFront
// below selects the actual target, and hidden samples are rejected. Display/OS
// refresh limits must still be distinguished from work performed per frame.
const front = () => {
  try {
    execFileSync("osascript", ["-e", 'tell application "Google Chrome" to activate']);
  } catch { /* not fatal: the control run will show if it did not take */ }
};

const CHROME = process.env.BENCH_CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PORT = Number(process.env.BENCH_PORT || 9338);
const ORIGIN = process.env.BENCH_ORIGIN || "http://127.0.0.1:5199/bench/";
//: One window size for every run. Frame cost scales with area, so a run in a
//: different window is not comparable with the one before it.
const WINDOW = process.env.BENCH_WINDOW || "1512,982";

const runsArgument = process.argv[2] || "[]";
const runs = JSON.parse(runsArgument.trim().startsWith("[") ? runsArgument : await readFile(runsArgument, "utf8"));
const seconds = Number(process.argv[3] || 6);

const chrome = spawn(CHROME, [
  `--remote-debugging-port=${PORT}`,
  `--window-size=${WINDOW}`,
  `--user-data-dir=/tmp/neo-bench-profile-${PORT}`,
  "--no-first-run",
  "--no-default-browser-check",
  "--disable-extensions",
  // Request normal scheduling; foreground/visibility checks remain necessary.
  "--disable-backgrounding-occluded-windows",
  "--disable-renderer-backgrounding",
  "--disable-background-timer-throttling",
  "about:blank",
], { stdio: "ignore", detached: false });

async function targets() {
  const res = await fetch(`http://127.0.0.1:${PORT}/json/list`);
  return res.json();
}

async function pageSocket() {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      const page = (await targets()).find((t) => t.type === "page");
      if (page?.webSocketDebuggerUrl) return page.webSocketDebuggerUrl;
    } catch { /* not up yet */ }
    await sleep(250);
  }
  throw new Error("Chrome never offered a page target");
}

function connect(url) {
  const socket = new WebSocket(url);
  const pending = new Map();
  let next = 1;
  const ready = new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  const listeners = new Map();
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.method) {
      const handler = listeners.get(message.method);
      if (handler) handler(message.params);
      return;
    }
    const slot = pending.get(message.id);
    if (!slot) return;
    pending.delete(message.id);
    if (message.error) slot.reject(new Error(message.error.message));
    else slot.resolve(message.result);
  });
  const send = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const id = next++;
      pending.set(id, { resolve, reject });
      socket.send(JSON.stringify({ id, method, params }));
    });
  const on = (method, handler) => listeners.set(method, handler);
  return { ready, send, on, close: () => socket.close() };
}

const evaluate = async (cdp, expression) => {
  const { result, exceptionDetails } = await cdp.send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (exceptionDetails) throw new Error(exceptionDetails.exception?.description || "eval failed");
  return result.value;
};

/**
 * Additional renderer/raster/compositor CPU trace scopes.
 *
 * A matching empty control is necessary: rAF intervals also reflect display
 * cadence, browser scheduling and occlusion. They do not diagnose the cause of
 * a 30Hz ceiling. Trace event timings add renderer/raster/compositor evidence;
 * they are nested CPU scopes, not GPU execution time, and must not be summed
 * into a single cost across event names or processes.
 */
async function trace(cdp, ms) {
  const events = [];
  cdp.on("Tracing.dataCollected", (params) => { events.push(...params.value); });
  const finished = new Promise((resolve) => cdp.on("Tracing.tracingComplete", resolve));
  await cdp.send("Tracing.start", {
    transferMode: "ReportEvents",
    traceConfig: {
      includedCategories: [
        "disabled-by-default-devtools.timeline",
        "disabled-by-default-devtools.timeline.frame",
        "devtools.timeline",
        "gpu",
        "viz",
        "cc",
      ],
    },
  });
  await sleep(ms);
  await cdp.send("Tracing.end");
  await finished;

  //: Summed by name, in microseconds as the trace reports them. Only complete
  //: events carry a duration; the instant ones are markers.
  const byName = new Map();
  for (const event of events) {
    if (typeof event.dur !== "number" || !event.name) continue;
    const slot = byName.get(event.name) || { name: event.name, totalUs: 0, count: 0, maxUs: 0 };
    slot.totalUs += event.dur;
    slot.count += 1;
    slot.maxUs = Math.max(slot.maxUs, event.dur);
    byName.set(event.name, slot);
  }
  return [...byName.values()]
    .map((s) => ({
      name: s.name,
      count: s.count,
      totalMs: +(s.totalUs / 1000).toFixed(1),
      meanMs: +(s.totalUs / s.count / 1000).toFixed(3),
      maxMs: +(s.maxUs / 1000).toFixed(3),
    }))
    .sort((a, b) => b.totalMs - a.totalMs)
    .slice(0, 60);
}

const results = [];
try {
  const cdp = connect(await pageSocket());
  await cdp.ready;
  await cdp.send("Page.enable");
  cdp.on("Runtime.exceptionThrown", ({ exceptionDetails }) => {
    process.stderr.write(`Browser error: ${exceptionDetails.exception?.description || exceptionDetails.text}\n`);
  });
  await cdp.send("Runtime.enable");
  const browserVersion = await cdp.send("Browser.getVersion");

  for (const run of runs) {
    await cdp.send("Emulation.setCPUThrottlingRate", { rate: run.cpu || 1 });
    await cdp.send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: run.still ? "reduce" : "no-preference" }] });
    if (run.viewport) {
      await cdp.send("Emulation.setDeviceMetricsOverride", {
        width: run.viewport.width, height: run.viewport.height,
        deviceScaleFactor: run.viewport.dpr || 2, mobile: false,
      });
    } else {
      await cdp.send("Emulation.clearDeviceMetricsOverride");
    }
    const url = `${run.origin || ORIGIN}?${new URLSearchParams({ ...run.params, seconds: run.seconds ?? seconds })}`;
    await cdp.send("Page.navigate", { url });
    //: Poll for the page's own readiness flag rather than a load event: the
    //: module graph, the stylesheet and the engine all have to be up before a
    //: measurement means anything.
    let ready = false;
    for (let attempt = 0; attempt < 80 && !ready; attempt += 1) {
      await sleep(250);
      try { ready = await evaluate(cdp, "window.__benchReady === true"); } catch { /* navigating */ }
    }
    if (!ready) throw new Error(`bench never became ready for ${run.label}`);
    if (run.style) await evaluate(cdp, `(() => { const style = document.createElement("style"); style.textContent = ${JSON.stringify(run.style)}; document.head.append(style); })()`);
    front();
    await cdp.send("Page.bringToFront");
    await sleep(400);
    if (run.interact) await evaluate(cdp, 'document.getElementById("bench-composer").focus()');
    let measuring = true;
    const input = (async () => {
      while (run.interact && measuring) {
        await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key: "a", code: "KeyA", text: "a", windowsVirtualKeyCode: 65 });
        await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: "a", code: "KeyA", windowsVirtualKeyCode: 65 });
        await sleep(180);
      }
    })();
    let measured;
    try { measured = await evaluate(cdp, "window.__benchRun()"); }
    finally { measuring = false; await input; }
    if (measured.config && (measured.config.visible !== "visible" || measured.config.hadHiddenTime || (!run.still && !measured.paintMs))) {
      throw new Error(`Invalid benchmark: ${run.label} was hidden or produced no paint samples`);
    }
    const work = process.env.BENCH_TRACE ? await trace(cdp, seconds * 1000) : null;
    if (run.screenshot) {
      const path = resolve(run.screenshot);
      await mkdir(dirname(path), { recursive: true });
      const shot = await cdp.send("Page.captureScreenshot", { format: "png" });
      await writeFile(path, Buffer.from(shot.data, "base64"));
    }
    results.push({ label: run.label, cpuThrottle: run.cpu || 1, browserVersion, ...measured, work });
    if (process.env.BENCH_OUTPUT) await writeFile(process.env.BENCH_OUTPUT, JSON.stringify(results, null, 2));
    process.stderr.write(`  measured: ${run.label}\n`);
  }
  cdp.close();
} finally {
  chrome.kill();
}

console.log(JSON.stringify(results, null, 2));
