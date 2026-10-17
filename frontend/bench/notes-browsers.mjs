/**
 * Runs bench/notes-browser.html in every browser on this machine and collects
 * what each one reports. The page posts its results back to a small local
 * server, so no WebDriver is needed and every browser runs the same code.
 *
 * Usage, from frontend/, with the dev server running (npm run dev):
 *   node bench/notes-browsers.mjs [chrome,brave,firefox,safari]
 *
 * Chrome, Brave and Firefox run headless in throwaway profiles. Safari has no
 * headless mode: the page opens in a new Safari window, which is closed again
 * afterwards. A browser that is not installed is reported as "not available",
 * never as passed.
 */
import { execFileSync, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";

const ORIGIN = process.env.SUITE_ORIGIN || "http://127.0.0.1:5173/";
const PAGE = new URL("bench/notes-browser.html", ORIGIN).href;
const TIMEOUT = Number(process.env.SUITE_TIMEOUT || 90_000);

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

const wanted = (process.argv[2] || Object.keys(BROWSERS).join(",")).split(",");
let waiting = null;
const server = createServer((request, response) => {
  response.setHeader("Access-Control-Allow-Origin", "*");
  response.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  response.setHeader("Access-Control-Allow-Headers", "*");
  if (request.method === "OPTIONS") return response.end();
  // The locked-notes vectors the server's tests use too, for the page's crypto checks.
  if (request.method === "GET" && request.url.startsWith("/vectors")) {
    readFile(new URL("../../tests/fixtures/notes_key_vectors.json", import.meta.url), "utf8").then(
      (text) => {
        response.setHeader("Content-Type", "application/json");
        response.end(text);
      },
      () => {
        response.statusCode = 404;
        response.end();
      },
    );
    return;
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
const report = `http://127.0.0.1:${server.address().port}/`;

const summary = {};
for (const name of wanted) {
  const browser = BROWSERS[name];
  if (!browser || !existsSync(browser.path)) {
    summary[name] = { status: "not available" };
    continue;
  }
  const url = `${PAGE}?report=${encodeURIComponent(report)}&run=${Date.now()}`;
  const result = new Promise((resolve) => (waiting = resolve));
  let child = null;
  let dir = null;
  if (browser.safari) {
    execFileSync("open", ["-a", "Safari", url]);
  } else {
    dir = await mkdtemp(join(tmpdir(), `neo-suite-${name}-`));
    child = spawn(browser.path, browser.args(url, dir), { stdio: "ignore" });
  }
  const outcome = await Promise.race([result, sleep(TIMEOUT, null, { ref: false }).then(() => null)]);
  if (browser.safari) {
    try {
      execFileSync("osascript", ["-e", 'tell application "Safari" to close (every tab of every window whose URL contains "notes-browser.html")']);
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
        status: outcome.failed === 0 ? "passed" : "failed",
        passed: outcome.passed,
        failed: outcome.failed,
        userAgent: outcome.userAgent,
        untestable: outcome.results.filter((item) => item.pass === null).map((item) => [item.suite, item.name, item.detail.reason]),
        failures: outcome.results.filter((item) => item.pass === false),
        timings: outcome.results.filter((item) => item.detail?.ms != null).map((item) => [item.name, item.detail.ms]),
        lossyFlags: outcome.results.filter((item) => item.suite === "fidelity-html").map((item) => [item.name, item.detail.same ? "kept" : item.detail.lossy ? "flagged" : "LOST"]),
      }
    : { status: "no report (timed out)" };
}
server.close();
console.log(JSON.stringify(summary, null, 2));
