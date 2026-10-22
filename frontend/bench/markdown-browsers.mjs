/**
 * Renders the same Markdown in every browser on this machine and checks the HTML matches
 * what Node produces, then times the current renderer against the one in git HEAD.
 *
 * The renderer is plain string-to-string JavaScript, so a browser can only differ through its
 * regular-expression engine; this proves it does not, on real model answers. Inputs come from
 * evaluation runs (answers Neo wrote) plus edge cases; nothing from a personal profile.
 *
 * Usage, from frontend/ (no dev server needed; this serves its own files):
 *   node bench/markdown-browsers.mjs <answers.json> [chrome,brave,firefox,safari]
 * where answers.json is a JSON array of strings (see the evaluation notes for how it is made).
 *
 * Chrome, Brave and Firefox run headless in throwaway profiles. Safari has no headless mode:
 * the page opens in a Safari window that is closed afterwards. A browser that is not installed
 * is reported as "not available", never as passed.
 */
import { execFileSync, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";

import { renderMarkdown } from "../src/markdown.js";

const TIMEOUT = Number(process.env.SUITE_TIMEOUT || 120_000);
const ROUNDS = Number(process.env.MD_ROUNDS || 20);
const BROWSERS = {
  chrome: {
    path: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    args: (url, dir) => ["--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", `--user-data-dir=${dir}`, url],
  },
  brave: {
    path: "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    args: (url, dir) => ["--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", `--user-data-dir=${dir}`, url],
  },
  firefox: {
    path: "/Applications/Firefox.app/Contents/MacOS/firefox",
    args: (url, dir) => ["--headless", "--no-remote", "--profile", dir, url],
  },
  safari: { path: "/Applications/Safari.app", safari: true },
};

const EDGE_CASES = [
  "**1. *Resident Evil Requiem***",
  "4.  **Explain the *process* (how it's done):**",
  "1. **Item**\n    * **Good *and* useful:** yes",
  "***both***",
  "**not\nclosed** here",
  "`a **b** c` and **d**",
  "**see [docs](https://example.com/x)**",
  "5 * 3 = 15 and 2*4=8",
  "Price: $5 * 2 = $10, a_b_c stays, 100% sure",
  "<script>alert(1)</script> & \"quotes\" 'single'",
  "**a ".repeat(2000) + "*b ".repeat(2000),
  "Streaming half **bol",
  "| a | b |\n|---|:-:|\n| **x** | *y* |",
  "```js\nconst x = a ** b; // **not bold**\n```",
  "Sources:\n[1] Title: https://example.com/a\n[2] Other: https://example.com/b",
];

const answersPath = process.argv[2];
const wanted = (process.argv[3] || Object.keys(BROWSERS).join(",")).split(",");
const answers = answersPath ? JSON.parse(await readFile(answersPath, "utf8")) : [];
const inputs = [...EDGE_CASES, ...answers];
const expected = inputs.map((text) => renderMarkdown(text));
const head = execFileSync("git", ["show", "HEAD:frontend/src/markdown.js"], { encoding: "utf8" });
const current = await readFile(new URL("../src/markdown.js", import.meta.url), "utf8");

const page = `<!doctype html><meta charset="utf-8"><title>markdown check</title><body>
<script type="module">
const params = new URLSearchParams(location.search);
const report = params.get("report");
const { renderMarkdown: now } = await import("/markdown-current.js");
const { renderMarkdown: before } = await import("/markdown-head.js");
const { inputs, expected, rounds } = await (await fetch("/vectors.json")).json();
const failures = [];
inputs.forEach((text, i) => { const html = now(text); if (html !== expected[i]) failures.push({ i, got: html.slice(0, 300), want: expected[i].slice(0, 300) }); });
function time(render) {
  const started = performance.now();
  for (let r = 0; r < rounds; r++) for (const text of inputs) render(text);
  return (performance.now() - started) / rounds;
}
time(now); time(before);
const msNow = [], msBefore = [];
for (let k = 0; k < 5; k++) { msBefore.push(time(before)); msNow.push(time(now)); }
const median = (v) => v.slice().sort((a, b) => a - b)[2];
await fetch(report, { method: "POST", body: JSON.stringify({ userAgent: navigator.userAgent, count: inputs.length,
  failures, msPerPassNow: median(msNow), msPerPassBefore: median(msBefore) }) });
document.body.textContent = "done";
</script>`;

let waiting = null;
const server = createServer(async (request, response) => {
  response.setHeader("Access-Control-Allow-Origin", "*");
  const url = request.url.split("?")[0];
  if (request.method === "GET") {
    const files = {
      "/page.html": ["text/html", page],
      "/markdown-current.js": ["text/javascript", current],
      "/markdown-head.js": ["text/javascript", head],
      "/vectors.json": ["application/json", JSON.stringify({ inputs, expected, rounds: ROUNDS })],
    };
    const file = files[url];
    if (!file) {
      response.statusCode = 404;
      return response.end();
    }
    response.setHeader("Content-Type", file[0]);
    return response.end(file[1]);
  }
  let body = "";
  request.on("data", (chunk) => (body += chunk));
  request.on("end", () => {
    response.end("ok");
    try {
      waiting?.(JSON.parse(body));
    } catch {
      /* ignore malformed */
    }
  });
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const origin = `http://127.0.0.1:${server.address().port}`;

const summary = { inputs: inputs.length, edgeCases: EDGE_CASES.length, rounds: ROUNDS };
const nodeNow = [], nodeBefore = [];
const { renderMarkdown: headRender } = await import(
  `data:text/javascript;base64,${Buffer.from(head).toString("base64")}`
);
for (let k = 0; k < 5; k++) {
  for (const [render, out] of [[headRender, nodeBefore], [renderMarkdown, nodeNow]]) {
    const started = performance.now();
    for (let r = 0; r < ROUNDS; r++) for (const text of inputs) render(text);
    out.push((performance.now() - started) / ROUNDS);
  }
}
const median = (v) => v.slice().sort((a, b) => a - b)[2];
summary.node = { msPerPassNow: median(nodeNow), msPerPassBefore: median(nodeBefore) };

for (const name of wanted) {
  const browser = BROWSERS[name];
  if (!browser || !existsSync(browser.path)) {
    summary[name] = { status: "not available" };
    continue;
  }
  const url = `${origin}/page.html?report=${encodeURIComponent(origin + "/report")}&run=${Date.now()}`;
  const result = new Promise((resolve) => (waiting = resolve));
  let child = null;
  let dir = null;
  if (browser.safari) {
    execFileSync("open", ["-a", "Safari", url]);
  } else {
    dir = await mkdtemp(join(tmpdir(), `neo-md-${name}-`));
    child = spawn(browser.path, browser.args(url, dir), { stdio: "ignore" });
  }
  const outcome = await Promise.race([result, sleep(TIMEOUT, null, { ref: false }).then(() => null)]);
  if (browser.safari) {
    try {
      execFileSync("osascript", ["-e", `tell application "Safari" to close (every tab of every window whose URL contains "${origin}")`]);
    } catch {
      /* the tab may already be gone */
    }
  } else {
    child.kill();
    await sleep(300);
    await rm(dir, { recursive: true, force: true });
  }
  summary[name] = outcome
    ? {
        status: outcome.failures.length === 0 ? "identical to node" : "DIFFERS",
        userAgent: outcome.userAgent,
        checked: outcome.count,
        failures: outcome.failures.slice(0, 5),
        msPerPassNow: Number(outcome.msPerPassNow.toFixed(2)),
        msPerPassBefore: Number(outcome.msPerPassBefore.toFixed(2)),
      }
    : { status: "no report (timed out)" };
}
server.close();
const out = process.env.MD_OUT;
if (out) await writeFile(out, JSON.stringify(summary, null, 2));
console.log(JSON.stringify(summary, null, 2));
