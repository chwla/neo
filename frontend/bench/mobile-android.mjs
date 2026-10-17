/**
 * Neo on Android: Chrome in the Android Emulator, driven with real input.
 *
 * Taps, long presses, swipes and typing go through the emulated touchscreen
 * and the on-screen keyboard (`adb shell input`), so Chrome sees what it sees
 * from a finger: real touch and pointer events, the soft keyboard resizing the
 * visual viewport, a real process kill. The page is read over Chrome's
 * DevTools socket. It runs against a production build in a disposable seeded
 * profile, deleted at the end whether the run passed or not.
 *
 * Emulator results are emulator results: an ARM64 Android image on an Apple
 * Silicon host with a software GPU. They say nothing about real-phone timing.
 *
 * Usage, from frontend/, with the backend on :8000 and a preview on :4173:
 *   npm run build && npm run preview -- --port 4173 --strictPort
 *   node bench/mobile-android.mjs [check,...]
 *
 * Environment: ANDROID_AVD (default neo-pixel7a-api34; booted headless if not
 * running), ANDROID_HOME (default ~/Library/Android/sdk), MOBILE_OUT (where
 * screenshots and the JSON report go), MOBILE_ORIGIN (default
 * http://localhost:4173/), MOBILE_THEME (a Neo theme id for the run).
 *
 * The page is served through `adb reverse`, so Chrome loads it from localhost:
 * a secure context, as WebCrypto (locked notes) requires.
 */
import { execFile, spawn } from "node:child_process";
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { promisify } from "node:util";
import { randomUUID } from "node:crypto";

const exec = promisify(execFile);
const HERE = new URL(".", import.meta.url).pathname;
const REPO = join(HERE, "..", "..");
const SDK = process.env.ANDROID_HOME || join(homedir(), "Library/Android/sdk");
const ADB = join(SDK, "platform-tools/adb");
const EMULATOR = join(SDK, "emulator/emulator");
const AVD = process.env.ANDROID_AVD || "neo-pixel7a-api34";
const ORIGIN = process.env.MOBILE_ORIGIN || "http://localhost:4173/";
const HOST_ORIGIN = ORIGIN.replace("localhost", "127.0.0.1");
const OUT = process.env.MOBILE_OUT || join(tmpdir(), `neo-mobile-android-${AVD}-${Date.now()}`);
const PYTHON = process.env.BENCH_PYTHON || join(REPO, ".venv/bin/python");
const DEVTOOLS_PORT = 9333;
const ALL_CHECKS = ["navigation", "editing", "lists", "touch", "attachments", "sketch", "locking", "recovery", "rotation", "accessibility", "performance"];
/** Test files the attachments check pushes to the device's Downloads. */
const FIXTURES = process.env.MOBILE_FIXTURES || null;
const CHECKS = (process.argv[2] || ALL_CHECKS.join(",")).split(",");

const report = { platform: "android-emulator", avd: AVD, origin: ORIGIN, checks: {}, notes: [] };
let serial = null;
let cdp = null;
let profile = null;
let calibration = null;
/** The phone shell (bar and drawer) or, on wider screens such as tablets, the sidebar. */
let phone = true;
/** The steps a check has reached, reported when it fails. */
let trail = [];
const step = (name) => trail.push(name);
/** What a check had found before it failed, when it keeps its findings here. */
let partial = null;

/* ------------------------------------------------------------- devices -- */

const adb = async (...args) => (await exec(ADB, ["-s", serial, ...args], { maxBuffer: 32 * 1024 * 1024 })).stdout;

async function runningSerialFor(avd) {
  const { stdout } = await exec(ADB, ["devices"]);
  for (const line of stdout.split("\n").slice(1)) {
    const [id, state] = line.trim().split(/\s+/);
    if (!id?.startsWith("emulator-") || state !== "device") continue;
    const name = (await exec(ADB, ["-s", id, "emu", "avd", "name"]).catch(() => ({ stdout: "" }))).stdout.split("\n")[0].trim();
    if (name === avd) return id;
  }
  return null;
}

async function bootDevice() {
  serial = await runningSerialFor(AVD);
  if (!serial) {
    const port = 5560 + Math.floor(Math.random() * 10) * 2;
    spawn(EMULATOR, ["-avd", AVD, "-no-window", "-no-audio", "-no-boot-anim", "-no-snapshot-save", "-gpu", "swiftshader_indirect", "-port", String(port)], {
      stdio: "ignore",
      detached: true,
    }).unref();
    serial = `emulator-${port}`;
    report.notes.push(`booted ${AVD} headless on ${serial}`);
  }
  await exec(ADB, ["-s", serial, "wait-for-device"], { timeout: 180_000 });
  for (let i = 0; i < 120; i += 1) {
    if ((await adb("shell", "getprop", "sys.boot_completed").catch(() => "")).trim() === "1") break;
    await sleep(2000);
  }
  report.device = {
    serial,
    android: (await adb("shell", "getprop", "ro.build.version.release")).trim(),
    model: (await adb("shell", "getprop", "ro.product.model")).trim(),
    screen: (await adb("shell", "wm", "size")).trim().split(": ").pop(),
    density: (await adb("shell", "wm", "density")).trim().split(": ").pop(),
    chrome: (await adb("shell", "dumpsys", "package", "com.android.chrome")).match(/versionName=(\S+)/)?.[1],
  };
}

/** Whatever Chrome or Android puts in front of the page (first-run, notifications), dismissed by its button text. */
async function dismissSystemDialogs() {
  for (let round = 0; round < 3; round += 1) {
    await adb("shell", "uiautomator", "dump", "/sdcard/neo-ui.xml").catch(() => "");
    const xml = await adb("shell", "cat", "/sdcard/neo-ui.xml").catch(() => "");
    const button = [...xml.matchAll(/text="(No thanks|No Thanks|Not now|Skip|Dismiss|Got it|Accept & continue|Use without an account)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"/g)][0];
    if (!button) return;
    const [, label, x1, y1, x2, y2] = button;
    await adb("shell", "input", "tap", String((Number(x1) + Number(x2)) >> 1), String((Number(y1) + Number(y2)) >> 1));
    report.notes.push(`dismissed a system dialog: ${label}`);
    await sleep(1200);
  }
}

/**
 * Chrome's own "Save password?" message, after a password is typed into a page:
 * browser UI over the top of the page (and Neo's bar). A person swipes it away;
 * so does this. Saving the password is not this test's business.
 */
async function dismissSavePassword() {
  const message = (await screenNodes()).find((node) => /^Save password\?$/.test(node.text));
  if (!message) return false;
  await adb("shell", "input", "swipe", String(message.x), String(message.y), String(message.x), String(Math.max(0, message.y - 400)), "200");
  await sleep(900);
  report.notes.push("swiped away Chrome's Save password message");
  return true;
}

/**
 * Chrome with Neo in front. `fresh` starts from a clean Chrome (this is a test
 * emulator: its Chrome holds nothing but earlier runs); otherwise Chrome is
 * relaunched as a person would reopen it, restoring the tab it had.
 */
async function openChrome(url, { fresh = false } = {}) {
  await adb("reverse", `tcp:${new URL(ORIGIN).port}`, `tcp:${new URL(ORIGIN).port}`);
  if (fresh) await adb("shell", "pm", "clear", "com.android.chrome");
  // Chrome's own debug-app mechanism: it then reads these flags, which skip its first-run screens.
  await adb("shell", 'echo "_ --disable-fre --no-default-browser-check --no-first-run --disable-signin-promo" > /data/local/tmp/chrome-command-line');
  await adb("shell", "am", "set-debug-app", "--persistent", "com.android.chrome");
  const start = ["shell", "am", "start", "-n", "com.android.chrome/com.google.android.apps.chrome.Main"];
  await adb(...start, ...(url ? ["-a", "android.intent.action.VIEW", "-d", url] : []));
  await sleep(4000);
  await dismissSystemDialogs();
  await adb("forward", `tcp:${DEVTOOLS_PORT}`, "localabstract:chrome_devtools_remote");
  let page = null;
  for (let i = 0; i < 80 && !page; i += 1) {
    // A cold device is slow to start Chrome, and its prompts can arrive late;
    // a first start can also drop the address it was given, so it is sent again.
    if (i && i % 10 === 0) await dismissSystemDialogs();
    if (url && (i === 25 || i === 55)) await adb(...start, "-a", "android.intent.action.VIEW", "-d", url);
    const targets = await fetch(`http://127.0.0.1:${DEVTOOLS_PORT}/json/list`).then((r) => r.json()).catch(() => []);
    // The tab on screen: taps go to it, so reads must too.
    for (const target of targets.filter((t) => t.type === "page" && t.url.startsWith(ORIGIN))) {
      const probe = connect(target.webSocketDebuggerUrl);
      try {
        await probe.ready;
        const { result } = await probe.send("Runtime.evaluate", { expression: "document.visibilityState", returnByValue: true });
        if (result.value === "visible") page = target;
      } catch {
        /* a tab that is going away */
      }
      probe.close();
      if (page) break;
    }
    if (!page) await sleep(500);
  }
  if (!page) {
    await screenshot("chrome-never-showed-neo").catch(() => {});
    await adb("shell", "uiautomator", "dump", "/sdcard/neo-ui.xml").catch(() => "");
    const texts = [...(await adb("shell", "cat", "/sdcard/neo-ui.xml").catch(() => "")).matchAll(/text="([^"]+)"/g)].map((m) => m[1]).slice(0, 12);
    throw new Error(`Chrome never showed Neo; on screen: ${JSON.stringify(texts)}`);
  }
  cdp?.close();
  cdp = connect(page.webSocketDebuggerUrl);
  await cdp.ready;
  await cdp.send("Runtime.enable");
  await cdp.send("Page.enable");
}

/* --------------------------------------------------------------- page -- */

function connect(url) {
  const socket = new WebSocket(url);
  const pending = new Map();
  let next = 1;
  const ready = new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    const slot = message.id && pending.get(message.id);
    if (!slot) return;
    pending.delete(message.id);
    if (message.error) slot.reject(new Error(message.error.message));
    else slot.resolve(message.result);
  });
  socket.addEventListener("close", () => {
    for (const slot of pending.values()) slot.reject(new Error("DevTools connection closed"));
    pending.clear();
  });
  const send = (method, params = {}) =>
    new Promise((resolve, reject) => {
      // A closed socket drops sends silently; fail instead of waiting forever.
      if (socket.readyState !== WebSocket.OPEN) {
        reject(new Error(`DevTools connection is ${socket.readyState === WebSocket.CONNECTING ? "not open yet" : "closed"}`));
        return;
      }
      const id = next++;
      // Android pauses a page that is not on screen (a system picker is up),
      // and a paused page does not answer: fail the check rather than wait forever.
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error(`DevTools ${method} got no answer in 30 s (is Chrome behind another app?)`));
      }, 30_000);
      pending.set(id, {
        resolve: (value) => { clearTimeout(timer); resolve(value); },
        reject: (error) => { clearTimeout(timer); reject(error); },
      });
      socket.send(JSON.stringify({ id, method, params }));
    });
  return { ready, send, close: () => socket.close() };
}

async function evaluate(expression) {
  const { result, exceptionDetails } = await cdp.send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (exceptionDetails) throw new Error(exceptionDetails.exception?.description || exceptionDetails.text);
  return result.value;
}

async function waitUntil(expression, ms = 10_000) {
  const begun = Date.now();
  while (Date.now() - begun < ms) {
    try {
      if (await evaluate(expression)) return true;
    } catch {
      /* navigating */
    }
    await sleep(150);
  }
  return false;
}

async function screenshot(name) {
  const { stdout } = await exec(ADB, ["-s", serial, "exec-out", "screencap", "-p"], { encoding: "buffer", maxBuffer: 64 * 1024 * 1024 });
  await writeFile(join(OUT, `${name}.png`), stdout);
}

/* -------------------------------------------------------------- input -- */

/**
 * A see-through cover over the whole page that records the next tap and keeps
 * it from the page, so calibrating never presses whatever is mid-screen (a
 * note row, the editor, a drawing).
 */
const CATCH_TAP = `(() => {
  window.__tapAt = null;
  document.getElementById("__neo-tap-catcher")?.remove();
  const cover = document.createElement("div");
  cover.id = "__neo-tap-catcher";
  cover.style.cssText = "position:fixed;inset:0;z-index:2147483647;background:transparent;touch-action:none";
  const swallow = (e) => { e.preventDefault(); e.stopPropagation(); };
  cover.addEventListener("pointerdown", (e) => { swallow(e); window.__tapAt = [e.clientX, e.clientY, e.pointerType]; setTimeout(() => cover.remove(), 400); });
  for (const type of ["touchstart", "mousedown", "click", "contextmenu"]) cover.addEventListener(type, swallow, { passive: false });
  document.body.append(cover);
  return true;
})()`;

/** Where the page sits on the screen: tap a known point and read back what the page saw. */
async function calibrate() {
  await evaluate(CATCH_TAP);
  const size = (await adb("shell", "wm", "size")).match(/(\d+)x(\d+)/);
  const [w, h] = [Number(size[1]), Number(size[2])];
  const rotation = await evaluate("screen.orientation?.angle ?? 0");
  // Mid-screen; on a turned screen higher up, where the page is even with a keyboard up.
  const [dx, dy] = rotation % 180 === 0 ? [Math.round(w / 2), Math.round(h * 0.55)] : [Math.round(h / 2), Math.round(w * 0.35)];
  // Mid-screen; a dialog of Chrome's own (it can arrive late) is dismissed and the tap retried.
  let seen = null;
  for (let attempt = 0; attempt < 3 && !seen; attempt += 1) {
    if (attempt) {
      await dismissSystemDialogs();
      await evaluate(CATCH_TAP);
    }
    await adb("shell", "input", "tap", String(dx), String(dy));
    await waitUntil("Array.isArray(window.__tapAt)", 3000);
    seen = await evaluate("window.__tapAt");
  }
  await evaluate(`document.getElementById("__neo-tap-catcher")?.remove(); true`);
  const dpr = await evaluate("devicePixelRatio");
  if (!seen) throw new Error("calibration tap never reached the page");
  const [panX, panY] = await evaluate("[visualViewport.offsetLeft, visualViewport.offsetTop]");
  calibration = { dpr, x: dx / dpr - (seen[0] - panX), y: dy / dpr - (seen[1] - panY), pointerType: seen[2] };
  report.calibration = calibration;
}

/**
 * Screen pixels for a point in page coordinates. While the keyboard is up the
 * browser may pan the visible area (the visual viewport) over the page, so the
 * pan is read at the moment of each tap.
 */
async function toDevice(x, y) {
  const [panX, panY] = await evaluate("[visualViewport.offsetLeft, visualViewport.offsetTop]");
  return [String(Math.round((x - panX + calibration.x) * calibration.dpr)), String(Math.round((y - panY + calibration.y) * calibration.dpr))];
}

async function centerOf(selector, index = 0) {
  return evaluate(`(() => {
    const el = [...document.querySelectorAll(${JSON.stringify(selector)})].filter((e) => e.getClientRects().length)[${index}];
    if (!el) return null;
    el.scrollIntoView({ block: "nearest", inline: "nearest" });
    const r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  })()`);
}

async function tap(selector, index = 0) {
  const at = await centerOf(selector, index);
  if (!at) throw new Error(`nothing to tap: ${selector}`);
  await adb("shell", "input", "tap", ...(await toDevice(at.x, at.y)));
  await sleep(450);
}

async function tapText(selector, text) {
  const index = await evaluate(`[...document.querySelectorAll(${JSON.stringify(selector)})].filter((e) => e.getClientRects().length).findIndex((e) => e.textContent.trim().startsWith(${JSON.stringify(text)}))`);
  if (index < 0) throw new Error(`no ${selector} reading ${text}`);
  await tap(selector, index);
}

async function longPress(selector, index = 0) {
  const at = await centerOf(selector, index);
  const [x, y] = await toDevice(at.x, at.y);
  await adb("shell", "input", "swipe", x, y, x, y, "900");
  await sleep(500);
}

async function swipe(fromX, fromY, toX, toY, ms = 300) {
  await adb("shell", "input", "swipe", ...(await toDevice(fromX, fromY)), ...(await toDevice(toX, toY)), String(ms));
  await sleep(500);
}

/**
 * One finger along a path of page points: down, moves, up, as the touchscreen
 * reports them (`input motionevent`), so a stroke can curve. Each move is a
 * separate report a few tens of milliseconds apart, like a slow finger.
 */
async function trace(points) {
  const [panX, panY] = await evaluate("[visualViewport.offsetLeft, visualViewport.offsetTop]");
  const at = ([x, y]) => `${Math.round((x - panX + calibration.x) * calibration.dpr)} ${Math.round((y - panY + calibration.y) * calibration.dpr)}`;
  const moves = points.slice(1).map((point) => `input motionevent MOVE ${at(point)}`);
  await adb("shell", [`input motionevent DOWN ${at(points[0])}`, ...moves, `input motionevent UP ${at(points.at(-1))}`].join("; "));
  await sleep(400);
}

/** Through the on-screen keyboard: only letters, digits, - and spaces, which every IME passes through unchanged. */
async function typeText(text) {
  for (const word of text.split(/(\s+)/)) {
    if (!word) continue;
    if (/^\s+$/.test(word)) await adb("shell", "input", "keyevent", "62");
    else await adb("shell", "input", "text", word);
  }
  await sleep(300);
}

const key = (code) => adb("shell", "input", "keyevent", String(code));

/* ------------------------------------------------------------ profile -- */

/** Where a run's profile credentials sit while it runs, so a run that dies is cleaned up by the next. */
const STATE = process.env.MOBILE_STATE || join(tmpdir(), "neo-mobile-android-profile.json");

async function cleanUpDeadRun() {
  const stale = await readFile(STATE, "utf8").then(JSON.parse, () => null);
  if (!stale?.id) return;
  const response = await fetch(new URL(`api/account-profiles/${stale.id}`, HOST_ORIGIN), {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: stale.password }),
  }).catch(() => null);
  report.notes.push(`profile left by an earlier run: ${response?.status === 204 || response?.status === 404 ? "deleted" : `not deleted (${response?.status})`}`);
  if (response?.status === 204 || response?.status === 404) await rm(STATE, { force: true });
}

async function createProfile() {
  const username = `and${Date.now().toString(36)}`;
  const password = `and-${randomUUID()}`;
  const created = await evaluate(`fetch("/api/account-profiles", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: ${JSON.stringify(username)}, password: ${JSON.stringify(password)} }) }).then((r) => r.json())`);
  profile = { id: created.profile.id, password };
  await writeFile(STATE, JSON.stringify(profile), { mode: 0o600 });
  await exec(PYTHON, [join(HERE, "seed_profile.py"), profile.id, "--chats", "3", "--messages", "4", "--long-chat-messages", "4", "--notes", "24"], { cwd: REPO });
  if (process.env.MOBILE_THEME) {
    await evaluate(`fetch("/api/appearance/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ theme: ${JSON.stringify(process.env.MOBILE_THEME)} }) }).then((r) => r.ok)`);
  }
  await cdp.send("Page.reload");
  if (!await waitUntil("Boolean(document.querySelector('.neo-mobile-bar, .system-nav'))", 30_000)) throw new Error("Neo never appeared");
  phone = await evaluate("Boolean(document.querySelector('.neo-mobile-bar')?.getClientRects().length)");
  report.layout = phone ? "phone (bar and drawer)" : "wide (sidebar)";
  await evaluate(`window.__errors = []; addEventListener("error", (e) => __errors.push(String(e.message))); addEventListener("unhandledrejection", (e) => __errors.push(String(e.reason)));`);
  await sleep(800);
}

async function deleteProfile() {
  if (!profile) return;
  const response = await fetch(new URL(`api/account-profiles/${profile.id}`, HOST_ORIGIN), {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: profile.password }),
  });
  report.profileDeleted = response.status === 204;
  if (report.profileDeleted) await rm(STATE, { force: true });
}

/* ------------------------------------------------- system screens -- */

/** What Android shows right now (the file pickers are not web pages): text, description, centre. */
async function screenNodes() {
  await adb("shell", "uiautomator", "dump", "/sdcard/neo-ui.xml").catch(() => "");
  const xml = await adb("shell", "cat", "/sdcard/neo-ui.xml").catch(() => "");
  return [...xml.matchAll(/<node [^>]*?text="([^"]*)"[^>]*?content-desc="([^"]*)"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"/g)].map(
    ([, text, desc, x1, y1, x2, y2]) => ({ text, desc, x: (Number(x1) + Number(x2)) >> 1, y: (Number(y1) + Number(y2)) >> 1 }),
  );
}

/**
 * Tap the first thing on screen whose text (or, with `byDescription`, its
 * description) matches, waiting for it to appear. Files are chosen by their
 * visible name: a thumbnail's description belongs to its preview button.
 */
async function tapScreen(pattern, { timeout = 10_000, byDescription = true } = {}) {
  const begun = Date.now();
  while (Date.now() - begun < timeout) {
    const found = (await screenNodes()).find((node) => pattern.test(node.text) || (byDescription && pattern.test(node.desc)));
    if (found) {
      await adb("shell", "input", "tap", String(found.x), String(found.y));
      await sleep(900);
      return found.text || found.desc;
    }
    await sleep(600);
  }
  const seen = (await screenNodes()).map((node) => node.text || node.desc).filter(Boolean).slice(0, 25);
  throw new Error(`nothing on screen matching ${pattern}; showing ${JSON.stringify(seen)}`);
}

/* ------------------------------------------------------------- shared -- */

async function closeDialogs() {
  for (let i = 0; i < 3 && await evaluate("Boolean(document.querySelector('.modal-backdrop'))"); i += 1) {
    await tap(".modal-backdrop .dialog-close, .modal-backdrop [aria-label^=Close]");
  }
}

/** The way to everything: the drawer on a phone, the sidebar (always there) on a wider screen. */
async function openDrawer() {
  await closeDialogs();
  if (!phone) return true;
  await tap(".neo-mobile-bar .neo-mobile-menu");
  return waitUntil("document.querySelector('.neo-app.nav-open') && getComputedStyle(document.getElementById('neo-nav-drawer')).visibility === 'visible'", 4000);
}
const nav = (selector) => (phone ? `#neo-nav-drawer ${selector}` : selector);
const SCREEN = "document.title.replace(/ \\u00b7 Neo$/, '')";
const onScreen = (label) => waitUntil(`${SCREEN} === ${JSON.stringify(label)}`, 8000);

async function goTo(label) {
  await openDrawer();
  await tapText(nav(".system-nav button"), label);
  return onScreen(label);
}

/** Back from a note to its list, where the layout shows one at a time. */
async function backToList() {
  if (await evaluate("Boolean(document.querySelector('.nw-listpane'))")) return;
  // From the folders, All Notes.
  if (await evaluate("Boolean(document.querySelector('.nw-sidebar')) && !document.querySelector('.nw-editorpane')")) {
    await tapText(".nw-side-item", "All Notes");
    await waitUntil("Boolean(document.querySelector('.nw-listpane'))", 4000);
    return;
  }
  if (await evaluate("Boolean(document.querySelector('.nw-toolbar .nw-back')?.getClientRects().length)")) await tap(".nw-toolbar .nw-back");
  await waitUntil("Boolean(document.querySelector('.nw-listpane'))", 4000);
}

/** The keyboard away: Done on a phone; elsewhere Escape, which leaves the text (never Back, which is history). */
async function dismissKeyboard() {
  if (!(await keyboard()).open) return;
  if (await evaluate(`Boolean(document.querySelector('.nw-toolbar [aria-label="Done editing"]')?.getClientRects().length)`)) await tap('.nw-toolbar [aria-label="Done editing"]');
  else await key(111);
  await sleep(600);
}

const keyboard = () => evaluate(`(() => {
  const vv = visualViewport;
  const nw = document.querySelector('.nw');
  return { open: nw?.dataset.keyboard === 'open', covered: Math.round(innerHeight - vv.height - vv.offsetTop), vvHeight: Math.round(vv.height), innerHeight };
})()`);

async function serverNoteBody(id) {
  return evaluate(`fetch("/api/notes/${id}?include_deleted=true").then((r) => r.json()).then((d) => d.note?.body || "")`);
}

async function openNoteTitled(title) {
  if (!await goTo("Notes")) throw new Error("Notes did not open");
  // Notes reopens where it was left; a note on screen goes back to its list.
  await dismissKeyboard();
  if (await evaluate("!document.querySelector('.nw-listpane')")) await backToList();
  await waitUntil("document.querySelectorAll('.nw-row[data-note-id]').length >= 3", 15_000);
  const index = await evaluate(`[...document.querySelectorAll('.nw-row[data-note-id]')].findIndex((r) => r.querySelector('.nw-row-title')?.textContent.trim() === ${JSON.stringify(title)})`);
  if (index < 0) throw new Error(`no row ${title}`);
  const id = await evaluate(`document.querySelectorAll('.nw-row[data-note-id]')[${index}].dataset.noteId`);
  await tap(".nw-row[data-note-id]", index);
  await waitUntil(`document.querySelector('.nw-prose h1')?.textContent.trim() === ${JSON.stringify(title)}`, 10_000);
  return id;
}

/** Caret at the end of the note's last block, by a real tap just after its text. */
async function tapEndOfNote() {
  const at = await evaluate(`(() => {
    const last = document.querySelector('.nw-prose').lastElementChild;
    last.scrollIntoView({ block: "center" });
    const range = document.createRange();
    range.selectNodeContents(last);
    const rects = [...range.getClientRects()];
    const r = rects.at(-1) || last.getBoundingClientRect();
    return { x: Math.min(r.right + 4, innerWidth - 8), y: r.top + r.height / 2 };
  })()`);
  await adb("shell", "input", "tap", ...(await toDevice(at.x, at.y)));
  await sleep(900);
}

/* ------------------------------------------------------------- checks -- */

const SCREENS = [
  ["Memory", "Memory"], ["Research", "Research"], ["Notes", "Notes"], ["Calendar", "Calendar"],
  ["Gallery", "Gallery"], ["Local Models", "Local Models"], ["Compare Models", "Compare Models"],
];

const checks = {
  /** Every Neo screen, by touch. */
  async navigation() {
    const screens = [];
    const record = async (name, title) => {
      const ok = await onScreen(title);
      await sleep(500);
      const state = await evaluate(`(() => {
        const root = document.querySelector('.neo-app > :not(.neo-sidebar):not(.neo-mobile-bar):not(.neo-nav-scrim)');
        return { height: Math.round(root?.getBoundingClientRect().height || 0), viewport: innerHeight,
          sideScroll: document.scrollingElement.scrollWidth > innerWidth + 1, drawerOpen: Boolean(document.querySelector('.neo-app.nav-open')) };
      })()`);
      screens.push({ name, ok: ok && !state.sideScroll && !state.drawerOpen && state.height >= state.viewport - 64, ...state });
      await screenshot(`nav-${name.toLowerCase().replace(/ /g, "-")}`);
    };
    for (const [label, title] of SCREENS) {
      await goTo(label).catch(() => {});
      await record(label, title);
    }
    for (const entry of ["Projects", "Files", "Repositories"]) {
      await openDrawer();
      await tap(nav(".sidebar-settings"));
      await waitUntil("Boolean(document.querySelector('.set-shell'))", 6000);
      let found = false;
      const categories = await evaluate("document.querySelectorAll('.set-nav button').length");
      for (let i = 0; i < categories && !found; i += 1) {
        await tap(".set-nav button", i);
        const index = await evaluate(`[...document.querySelectorAll('.set-detail-list button')].findIndex((b) => b.textContent.trim().startsWith(${JSON.stringify(entry)}))`);
        if (index >= 0) {
          await tap(".set-detail-list button", index);
          found = true;
        }
      }
      await record(entry, entry);
    }
    // Tasks: only the command palette leads there.
    await closeDialogs();
    await tap(phone ? ".neo-mobile-bar .neo-mobile-palette" : ".sidebar-palette-button");
    await waitUntil("document.activeElement?.classList.contains('cmdk-input')", 5000);
    await typeText("Go to Tasks");
    await tapText(".cmdk-row", "Go to Tasks");
    await record("Tasks", "Tasks");
    return { passed: screens.every((s) => s.ok), screens };
  },

  /** Caret, typing through the keyboard, the docked bar, undo and redo, Done, selection and bold, autosave. */
  async editing() {
    const out = {};
    const id = await openNoteTitled("Bench note 003");
    step("opened note");
    await tapEndOfNote();
    step("tapped end of note");
    out.caretInNote = await evaluate("document.activeElement?.classList.contains('nw-prose') && getSelection().rangeCount > 0 && document.querySelector('.nw-prose').contains(getSelection().anchorNode)");
    await waitUntil("document.querySelector('.nw')?.dataset.keyboard === 'open'", 6000);
    out.keyboard = await keyboard();
    // The bar docks above the keyboard, at the bottom of what can be seen, on any touch screen.
    out.toolbarDocked = await evaluate(`(() => {
      const bar = document.querySelector('.nw-toolbar').getBoundingClientRect();
      return Math.abs(bar.bottom - (visualViewport.height + visualViewport.offsetTop)) <= 6;
    })()`);
    out.editControlsShown = await evaluate(`["Undo", "Redo", "Done editing"].every((label) => document.querySelector('.nw-toolbar [aria-label="' + label + '"]')?.getClientRects().length > 0)`);
    out.navHiddenWhileTyping = await evaluate("!document.querySelector('.nw-toolbar [aria-label=\"More\"]')?.getClientRects().length");
    await screenshot("edit-keyboard");
    const token = `android${Date.now().toString(36)}`;
    await key(66); // Enter: a new line
    await typeText(`typed on android ${token}`);
    step("typed");
    // The keyboard may capitalise or correct ordinary words; the token is left alone.
    out.typedShows = await evaluate(`document.querySelector('.nw-prose').textContent.includes(${JSON.stringify(token)})`);
    // Undo removes the last of the typing, redo puts it back.
    await tap('.nw-toolbar [aria-label="Undo"]');
    const afterUndo = await evaluate(`document.querySelector('.nw-prose').textContent.includes(${JSON.stringify(token)})`);
    await tap('.nw-toolbar [aria-label="Redo"]');
    const afterRedo = await evaluate(`document.querySelector('.nw-prose').textContent.includes(${JSON.stringify(token)})`);
    out.undoRedo = !afterUndo && afterRedo;
    step("undo and redo");
    // Saved without anyone asking.
    const begun = Date.now();
    let saved = false;
    while (!saved && Date.now() - begun < 12_000) {
      saved = (await serverNoteBody(id)).includes(token);
      if (!saved) await sleep(500);
    }
    out.autosaved = saved;
    step("autosave checked");
    out.autosaveMs = saved ? Date.now() - begun : null;
    // Selection by double tap, then Bold from the format panel.
    const word = await evaluate(`(() => {
      const walker = document.createTreeWalker(document.querySelector('.nw-prose'), NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) {
        const at = node.textContent.indexOf(${JSON.stringify(token)});
        if (at >= 0) {
          const range = document.createRange();
          range.setStart(node, at + 2);
          range.setEnd(node, at + 3);
          const r = range.getBoundingClientRect();
          return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
        }
      }
      return null;
    })()`);
    // Android selects a word on a long press.
    out.beforeLongPress = await evaluate(`({ word: ${JSON.stringify(word)},
      bar: (() => { const r = document.querySelector('.nw-toolbar').getBoundingClientRect(); return [Math.round(r.top), Math.round(r.bottom)]; })(),
      viewport: [visualViewport.offsetTop, visualViewport.height], keyboard: document.querySelector('.nw')?.dataset.keyboard,
      focused: document.activeElement?.className })`);
    await screenshot("edit-before-longpress");
    const [wx, wy] = await toDevice(word.x, word.y);
    out.beforeLongPress.device = [wx, wy];
    await adb("shell", "input", "swipe", wx, wy, wx, wy, "800");
    step("long-pressed a word");
    await sleep(900);
    out.selection = await evaluate("getSelection().toString()");
    out.longPressSelects = out.selection.includes(token);
    await screenshot("edit-longpress");
    await screenshot("edit-selection");
    await tap('.nw-toolbar [aria-label="Format"]');
    step("tapped Format");
    await waitUntil("Boolean(document.querySelector('.nw-format'))", 3000);
    await screenshot("edit-format");
    await tap('.nw-format [aria-label="Bold"]');
    out.boldApplied = await evaluate(`[...document.querySelectorAll('.nw-prose strong')].some((s) => s.textContent.includes(${JSON.stringify(token)}))`);
    step("bold checked");
    // Escape closes the panel (Android's back would be history navigation once the keyboard is down).
    await key(111);
    step("pressed back");
    await sleep(400);
    // Done (or Android's back) puts the keyboard away.
    await dismissKeyboard();
    await sleep(400);
    out.keyboardAfterDone = await keyboard();
    out.toolbarBackOnTop = await evaluate("document.querySelector('.nw-toolbar').getBoundingClientRect().top < 140");
    out.passed = Boolean(out.caretInNote && out.keyboard.open && out.toolbarDocked && out.editControlsShown && out.navHiddenWhileTyping && out.typedShows && out.undoRedo && out.autosaved && out.longPressSelects && out.boldApplied && !out.keyboardAfterDone.open && out.toolbarBackOnTop);
    return out;
  },

  /** Bullets, numbers, checklists, nesting and a table, all from the toolbar by touch. */
  async lists() {
    const out = {};
    await openNoteTitled("Bench note 004");
    await tapEndOfNote();
    await waitUntil("document.querySelector('.nw')?.dataset.keyboard === 'open'", 6000);
    // The list the caret is in (the editor keeps an empty paragraph after the last block).
    const caretList = `(() => { const n = getSelection().anchorNode; return (n?.nodeType === 1 ? n : n?.parentElement)?.closest('ul, ol'); })()`;
    const block = (selector) => evaluate(`Boolean(${caretList}?.matches(${JSON.stringify(selector)}))`);
    const formatTap = async (label) => {
      await tap('.nw-toolbar [aria-label="Format"]');
      await waitUntil("Boolean(document.querySelector('.nw-format'))", 3000);
      await tap(`.nw-format [aria-label="${label}"]`);
      await sleep(300);
    };
    await key(66);
    await typeText("first bullet");
    await formatTap("Bulleted list");
    out.bullets = await block("ul:not([data-type=taskList])");
    await key(66);
    await typeText("nested bullet");
    await formatTap("Indent");
    out.nested = await evaluate(`Boolean(${caretList}?.parentElement?.closest('li'))`);
    await key(66);
    await key(66);
    await key(66);
    await typeText("first number");
    await formatTap("Numbered list");
    out.numbers = await block("ol");
    await key(66);
    await key(66);
    await typeText("a task");
    await tap('.nw-toolbar [aria-label="Checklist"]');
    out.checklist = await block("ul[data-type=taskList]");
    await key(66);
    await typeText("another task");
    out.checklistContinues = await evaluate(`(${caretList}?.querySelectorAll('li').length ?? 0) >= 2`);
    await key(66);
    await key(66);
    await tap('.nw-toolbar [aria-label="Table"]');
    out.table = await evaluate("Boolean(document.querySelector('.nw-prose table'))");
    await typeText("cell");
    out.typedInCell = await evaluate("[...document.querySelectorAll('.nw-prose td, .nw-prose th')].some((c) => c.textContent.includes('cell'))");
    out.tableBar = await evaluate("Boolean(document.querySelector('.nw-tablebar'))");
    await screenshot("lists");
    await dismissKeyboard();
    out.passed = Object.values(out).every(Boolean);
    return out;
  },

  /** Check circles without the keyboard, long-press menus, and scrolling by swipe. */
  async touch() {
    const out = {};
    await openNoteTitled("Bench note 013");
    await dismissKeyboard();
    await sleep(500);
    const before = await evaluate("document.querySelector('ul[data-type=taskList] input')?.checked");
    await tap("ul[data-type=taskList] li > label");
    await sleep(800);
    await screenshot("touch-check");
    out.checkToggled = (await evaluate("document.querySelector('ul[data-type=taskList] input')?.checked")) === !before;
    out.keyboardStayedDown = !(await keyboard()).open;
    await backToList();
    const openBefore = await evaluate("document.querySelector('.nw-prose h1')?.textContent.trim() || null");
    const pressedTitle = await evaluate("[...document.querySelectorAll('.nw-row[data-note-id]')][2]?.querySelector('.nw-row-title')?.textContent.trim()");
    await longPress(".nw-row[data-note-id]", 2);
    out.longPressMenu = await waitUntil("Boolean(document.querySelector('.nw-menu'))", 3000);
    // Not opened: on a phone the list is still showing; wider, the note on screen is unchanged.
    out.longPressDidNotOpen = phone
      ? await evaluate("!document.querySelector('.nw-editorpane')")
      : (await evaluate("document.querySelector('.nw-prose h1')?.textContent.trim() || null")) === openBefore && openBefore !== pressedTitle;
    await screenshot("touch-longpress");
    if (out.longPressMenu) {
      await tapText(".nw-menu [role=menuitem]", "Pin Note");
      out.pinnedFromMenu = await waitUntil("Boolean(document.querySelector('.nw-row .nw-row-pin'))", 4000);
    }
    const list = await evaluate("(() => { const r = document.querySelector('.nw-rows').getBoundingClientRect(); return { x: r.left + r.width / 2, top: r.top, bottom: r.bottom }; })()");
    const scrolledFrom = await evaluate("document.querySelector('.nw-rows').scrollTop");
    await swipe(list.x, list.bottom - 80, list.x, list.top + 80, 250);
    await sleep(800);
    out.swipeScrolls = (await evaluate("document.querySelector('.nw-rows').scrollTop")) > scrolledFrom + 40;
    out.passed = Object.values(out).every(Boolean);
    return out;
  },

  /**
   * A photo and a file through Android's own pickers, as a person picks them;
   * the file opened again from its chip.
   */
  async attachments() {
    const out = {};
    if (!FIXTURES) return { passed: null, skipped: "MOBILE_FIXTURES (a folder with neo-photo.jpg and neo-tickets.pdf) not given" };
    for (const name of ["neo-photo.jpg", "neo-tickets.pdf"]) await adb("push", join(FIXTURES, name), `/sdcard/Download/${name}`);
    await adb("shell", "content", "call", "--uri", "content://media", "--method", "scan_volume", "--arg", "external_primary");
    const id = await openNoteTitled("Bench note 011");
    await tapEndOfNote();
    step("note open");
    // The photo.
    await tap('.nw-toolbar [aria-label="Attach"]');
    await tapText(".nw-menu [role=menuitem]", "Choose Photo");
    step("asked for a photo");
    await sleep(1500);
    out.photoPickerScreens = [];
    out.photoPickerScreens.push((await screenNodes()).map((n) => n.text || n.desc).filter(Boolean).slice(0, 20));
    await screenshot("attach-photo-picker");
    // Chrome may first ask where from; then the picker. Whatever it is, the file is chosen by its name.
    const route = [];
    for (let hop = 0; hop < 4; hop += 1) {
      const names = (await screenNodes()).map((n) => `${n.text}|${n.desc}`);
      if (names.some((n) => /^neo-photo\.jpg\|/.test(n))) {
        route.push(await tapScreen(/^neo-photo\.jpg$/, { byDescription: false }));
        break;
      }
      // Asked for the camera as well (a photo input can offer it): not needed to pick a photo.
      if (names.some((n) => /take pictures/.test(n))) {
        route.push(await tapScreen(/^Don.t allow$/));
        continue;
      }
      // Android's own photo picker (newer versions) names photos by date, not file:
      // the one pushed for this run is the newest; it is added with "Add".
      if (names.some((n) => /Photo taken on/.test(n))) {
        route.push(await tapScreen(/^Photo taken on/));
        route.push(await tapScreen(/^(Add\b|Done$)/, { timeout: 5000 }));
        break;
      }
      // Android asks once whether Chrome may see photos; a person allows it.
      const next = [/^Allow all$/, /^Allow$/, /^Files$/, /^Browse$/, /^Documents$/, /^Downloads?$/, /Show roots/].find((pattern) => names.some((n) => n.split("|").some((part) => pattern.test(part))));
      if (!next) break;
      route.push(await tapScreen(next));
      out.photoPickerScreens.push((await screenNodes()).map((n) => n.text || n.desc).filter(Boolean).slice(0, 20));
    }
    out.photoRoute = route;
    // Some pickers want a confirming tap.
    await tapScreen(/^(Done|Select|Add|Open)$/, { timeout: 2500 }).catch(() => null);
    step("picked the photo");
    out.photoShown = await waitUntil("[...document.querySelectorAll('.nw-prose img')].some((i) => i.complete && i.naturalWidth > 0)", 20_000);
    step(`photo shown: ${out.photoShown}; route ${JSON.stringify(out.photoRoute)}; screens ${JSON.stringify(out.photoPickerScreens)}`);
    out.photoSize = await evaluate("(() => { const i = [...document.querySelectorAll('.nw-prose img')].at(-1); return i ? [i.naturalWidth, i.naturalHeight] : null; })()");
    await screenshot("attach-photo");
    // The file.
    await tapEndOfNote();
    await tap('.nw-toolbar [aria-label="Attach"]');
    await tapText(".nw-menu [role=menuitem]", "Attach File");
    step("asked for a file");
    await sleep(1500);
    out.filePickerScreens = [(await screenNodes()).map((n) => n.text || n.desc).filter(Boolean).slice(0, 20)];
    await screenshot("attach-file-picker");
    const fileRoute = [];
    for (let hop = 0; hop < 4; hop += 1) {
      const names = (await screenNodes()).map((n) => `${n.text}|${n.desc}`);
      if (names.some((n) => /^neo-tickets\.pdf\|/.test(n))) {
        fileRoute.push(await tapScreen(/^neo-tickets\.pdf$/, { byDescription: false }));
        break;
      }
      // An input open to any file can also offer the camera, so Chrome asks for it: not needed here.
      if (names.some((n) => /take pictures/.test(n))) {
        fileRoute.push(await tapScreen(/^Don.t allow$/));
        continue;
      }
      const next = [/^Allow all$/, /^Allow$/, /^Files$/, /^Browse$/, /^Documents$/, /^Downloads?$/, /Show roots/].find((pattern) => names.some((n) => n.split("|").some((part) => pattern.test(part))));
      if (!next) break;
      fileRoute.push(await tapScreen(next));
      out.filePickerScreens.push((await screenNodes()).map((n) => n.text || n.desc).filter(Boolean).slice(0, 20));
    }
    out.fileRoute = fileRoute;
    await tapScreen(/^(Done|Select|Open)$/, { timeout: 2500 }).catch(() => null);
    step("picked the file");
    out.fileChip = await waitUntil("[...document.querySelectorAll('.nw-prose .nw-file-name')].some((n) => /neo-tickets/.test(n.textContent))", 20_000);
    // Saved, with the picture as the note's cover.
    let saved = null;
    const begun = Date.now();
    while (Date.now() - begun < 15_000) {
      saved = await evaluate(`fetch("/api/notes/${id}").then((r) => r.json()).then((d) => d.note)`);
      if (saved?.has_images && saved?.has_files) break;
      await sleep(800);
    }
    out.saved = { images: saved?.has_images, files: saved?.has_files, cover: Boolean(saved?.cover_file_id) };
    // Opening the chip downloads the file.
    // One argument, so the device's shell sees the quotes around a name with brackets in it.
    await adb("shell", "rm -f '/sdcard/Download/neo-tickets (1).pdf'");
    await dismissKeyboard();
    const pages = async () => (await (await fetch(`http://127.0.0.1:${DEVTOOLS_PORT}/json/list`)).json()).filter((t) => t.type === "page").map((t) => t.url);
    const pagesBefore = (await pages()).length;
    await tap(".nw-prose .nw-file-link");
    await sleep(4000);
    await dismissSystemDialogs();
    // A download stays in the note: no tab of its own left in front of it.
    out.pagesAfterDownload = await pages();
    out.noNewTab = out.pagesAfterDownload.length === pagesBefore;
    const downloads = await adb("shell", "ls", "/sdcard/Download/");
    out.downloaded = /neo-tickets.*\(1\)\.pdf|neo-tickets-1\.pdf/.test(downloads) || downloads.split("\n").filter((name) => name.startsWith("neo-tickets")).length > 1;
    out.downloads = downloads.split("\n").filter(Boolean);
    await screenshot("attach-download");
    out.passed = Boolean(out.photoShown && out.fileChip && out.saved.images && out.saved.files && out.saved.cover && out.downloaded && out.noNewTab);
    return out;
  },

  /** Typing while the server is unreachable, then Chrome killed outright, then recovery. */
  async recovery() {
    const out = {};
    const id = await openNoteTitled("Bench note 005");
    await tapEndOfNote();
    await waitUntil("document.querySelector('.nw')?.dataset.keyboard === 'open'", 6000);
    const port = new URL(ORIGIN).port;
    // Offline as far as this phone is concerned: the tunnel to the server is gone.
    await adb("reverse", "--remove", `tcp:${port}`);
    const token = `offline${Date.now().toString(36)}`;
    await key(66);
    await typeText(`kept while offline ${token}`);
    await sleep(2500);
    out.offlineStatusShown = await evaluate("document.querySelector('.nw-banner')?.textContent || document.querySelector('.nw')?.textContent.includes('Saved on this device') || null");
    // What the recovery journal holds for this note, read straight from IndexedDB.
    const journalHolds = () => evaluate(`new Promise((resolve) => {
      const open = indexedDB.open("neo-notes-recovery:${profile.id}");
      open.onerror = () => resolve({ error: String(open.error) });
      open.onsuccess = () => {
        const db = open.result;
        if (![...db.objectStoreNames].includes("entries")) { db.close(); resolve({ stores: [...db.objectStoreNames] }); return; }
        const all = db.transaction("entries").objectStore("entries").getAll();
        all.onsuccess = () => { const hit = all.result.filter((e) => JSON.stringify(e).includes(${JSON.stringify(token)})); db.close(); resolve({ entries: all.result.length, withToken: hit.length }); };
      };
    })`);
    out.journalBeforeKill = await journalHolds();
    // A real process kill, not a tab close: nothing gets to run on the way out.
    await adb("shell", "am", "force-stop", "com.android.chrome");
    await adb("reverse", `tcp:${port}`, `tcp:${port}`);
    await openChrome(null);
    // A browser killed seconds after signing in may not have written the new
    // session cookie to disk yet (Chrome saves cookies every so often). Then,
    // as a person would, sign in again; recovery happens in the profile.
    await waitUntil("Boolean(document.querySelector('.neo-mobile-bar, .system-nav, .profile-picker'))", 30_000);
    out.signedOutByKill = await evaluate("Boolean(document.querySelector('.profile-picker'))");
    if (out.signedOutByKill) {
      await evaluate(`fetch("/api/account-profiles/${profile.id}/unlock", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: ${JSON.stringify(profile.password)} }) }).then((r) => r.status)`);
      await cdp.send("Page.reload");
    }
    await waitUntil("Boolean(document.querySelector('.neo-mobile-bar, .system-nav'))", 30_000);
    await calibrate();
    out.journalAfterRestart = await journalHolds();
    // Recovery replays when Notes opens.
    await goTo("Notes");
    const begun = Date.now();
    let recovered = false;
    while (!recovered && Date.now() - begun < 20_000) {
      recovered = (await serverNoteBody(id)).includes(token);
      if (!recovered) await sleep(800);
    }
    out.recoveredAfterKill = recovered;
    // Not in the note: kept as a conflict instead (preserved, to be resolved), or lost.
    if (!recovered) {
      out.keptAsConflict = await evaluate(`fetch("/api/notes/${id}/conflicts").then((r) => r.json()).then((list) => list.some((c) => JSON.stringify(c).includes(${JSON.stringify(token)})))`).catch(() => null);
    }
    await screenshot("recovery");
    out.passed = Boolean(out.recoveredAfterKill);
    return out;
  },

  /** Landscape and back. */
  /** A quarter turn and back: the other orientation from the device's own. */
  async rotation() {
    const out = {};
    await goTo("Notes");
    const shape = "({ w: innerWidth, h: innerHeight, sideScroll: document.scrollingElement.scrollWidth > innerWidth + 1, notes: Boolean(document.querySelector('.nw')), mode: document.querySelector('.nw')?.dataset.mode })";
    out.natural = await evaluate(shape);
    await adb("shell", "settings", "put", "system", "accelerometer_rotation", "0");
    await adb("shell", "settings", "put", "system", "user_rotation", "1");
    await sleep(2500);
    out.turned = await evaluate(shape);
    await screenshot("rotation-turned");
    await adb("shell", "settings", "put", "system", "user_rotation", "0");
    await sleep(2500);
    out.back = await evaluate(shape);
    await calibrate();
    const flipped = (a, b) => (a.w > a.h) !== (b.w > b.h);
    out.passed = flipped(out.natural, out.turned) && !flipped(out.natural, out.back) && [out.turned, out.back].every((s) => s.notes && !s.sideScroll);
    return out;
  },

  /**
   * A sketch drawn with a finger: real touch strokes (a line and a loop),
   * undo, redo and the eraser, saved into the note where they were drawn;
   * then the screen turned, the drawing the same shape, and drawn on again
   * sideways.
   */
  async sketch() {
    const out = (partial = {});
    const { decodePoints } = await import("../src/notes/sketch/strokes.js");
    const id = await openNoteTitled("Bench note 012");
    const textBefore = await serverNoteBody(id);
    await tapEndOfNote();
    step("note open");
    await tap('.nw-toolbar [aria-label="Attach"]');
    await tapText(".nw-menu [role=menuitem]", "Sketch");
    const areaReady = "(() => { const r = document.querySelector('.nw-sketch-input')?.getBoundingClientRect(); return Boolean(r && r.width > 100 && r.height > 100); })()";
    if (!await waitUntil(areaReady, 10_000)) throw new Error("the drawing area never appeared");
    // The keyboard going down resizes the drawing area.
    await sleep(1500);
    step("sketch open");
    out.keyboardWhileDrawing = await keyboard();
    const areaRect = () => evaluate("(() => { const r = document.querySelector('.nw-sketch-input').getBoundingClientRect(); return { x: r.left, y: r.top, w: r.width, h: r.height }; })()");
    const inArea = (r, fx, fy) => [r.x + r.w * fx, r.y + r.h * fy];
    await evaluate(`window.__sketchPointers = []; window.__sketchEvents = [];
      const input = document.querySelector('.nw-sketch-input');
      input.addEventListener('pointerdown', (e) => __sketchPointers.push(e.pointerType));
      for (const type of ['pointerdown', 'pointerup', 'pointercancel', 'lostpointercapture']) input.addEventListener(type, (e) => __sketchEvents.push(type + ':' + e.pointerId));
      let moves = 0; input.addEventListener('pointermove', () => { moves += 1; window.__sketchMoves = moves; });
      // Samples the page was given per stroke: the press and every (coalesced) move.
      window.__sketchSamples = [];
      input.addEventListener('pointerdown', () => __sketchSamples.push(1));
      input.addEventListener('pointermove', (e) => { if (e.buttons && __sketchSamples.length) __sketchSamples[__sketchSamples.length - 1] += e.getCoalescedEvents?.().length || 1; });
      true`);
    let area = await areaRect();
    out.firstArea = { ...area, ratio: +(area.h / area.w).toFixed(3) };
    out.events = [];
    const logEvents = async (label) => out.events.push([label, await evaluate("window.__sketchEvents.splice(0).join(' ') + ' moves=' + (window.__sketchMoves || 0)")]);
    await trace([inArea(area, 0.15, 0.3), inArea(area, 0.4, 0.31), inArea(area, 0.6, 0.33), inArea(area, 0.85, 0.35)]);
    await logEvents("line");
    // The colours are all there to see, and one is picked with a finger for the loop.
    out.swatches = await evaluate(`[...document.querySelectorAll('.nw-sketch-color')].map((b) => getComputedStyle(b, '::before').backgroundColor)`);
    await tap('.nw-sketch-color[aria-label="Pink"]');
    await trace(Array.from({ length: 13 }, (_, i) => inArea(area, 0.5 + 0.2 * Math.cos((i / 12) * 2 * Math.PI), 0.6 + 0.12 * Math.sin((i / 12) * 2 * Math.PI))));
    await logEvents("loop");
    step("drawn");
    // A third stroke, undone at once, as a person does (the tap 200 ms after the
    // finger lifts, in one go on the device so the gap is exact), redone, then
    // rubbed out.
    const history = () => evaluate(`({ undo: !document.querySelector('.nw-sketch-editor [aria-label="Undo"]').disabled, redo: !document.querySelector('.nw-sketch-editor [aria-label="Redo"]').disabled })`);
    const undoAt = await centerOf('.nw-sketch-editor [aria-label="Undo"]');
    const [ux, uy] = await toDevice(undoAt.x, undoAt.y);
    const third = [];
    for (const point of [inArea(area, 0.2, 0.88), inArea(area, 0.5, 0.9), inArea(area, 0.8, 0.88)]) third.push((await toDevice(...point)).join(" "));
    await adb("shell", `input swipe ${third[0]} ${third[2]} 300; sleep 0.2; input tap ${ux} ${uy}`);
    await sleep(800);
    await logEvents("third");
    out.afterUndo = await history();
    await tap('.nw-sketch-editor [aria-label="Redo"]');
    out.afterRedo = await history();
    await tapText(".nw-sketch-tool", "Eraser");
    await trace([inArea(area, 0.48, 0.85), inArea(area, 0.5, 0.9), inArea(area, 0.52, 0.95)]);
    await tapText(".nw-sketch-tool", "Pen");
    step("undo, redo, eraser");
    out.pointerTypes = [...new Set(await evaluate("window.__sketchPointers"))];
    out.smallTargets = await evaluate(`[...document.querySelectorAll('.nw-sketch-editor button')].map((b) => [b.getAttribute('aria-label') || b.textContent.trim(), b.getBoundingClientRect()]).filter(([, r]) => r.width < 44 || r.height < 44).map(([name, r]) => name + ' ' + Math.round(r.width) + 'x' + Math.round(r.height))`);
    await screenshot("sketch-drawn");
    await tap(".nw-sketch-done");
    await waitUntil("!document.querySelector('.nw-sketch-editor')", 5000);
    step("done");
    // Back in the note with no keyboard: finishing a drawing is not starting to type.
    await sleep(1500);
    out.afterDone = { keyboard: await keyboard(), focus: await evaluate("(() => { const a = document.activeElement; return a ? a.tagName + '.' + String(a.className) : null; })()") };

    const findSketch = (node) => node?.type === "sketch" ? node : (node?.content || []).map(findSketch).find(Boolean) ?? null;
    const savedWith = async (count) => {
      const begun = Date.now();
      while (Date.now() - begun < 15_000) {
        const note = await evaluate(`fetch("/api/notes/${id}").then((r) => r.json()).then((d) => d.note)`);
        const node = findSketch(note.doc);
        if (node?.attrs.strokes.length === count) return { note, node };
        out.lastSaved = { revision: note.revision, format: note.content_format, sketch: node ? node.attrs.strokes.map((stroke) => `${stroke.tool}:${stroke.color}`) : null };
        await sleep(500);
      }
      return null;
    };
    const span = (points, axis) => [Math.min(...points.map((p) => p[axis])), Math.max(...points.map((p) => p[axis]))];
    let saved = await savedWith(2);
    if (!saved) throw new Error("the sketch never saved with its line and loop (the third stroke erased)");
    const aspect = saved.node.attrs.aspect;
    const height = 10000 * aspect;
    const [line, loop] = saved.node.attrs.strokes.map((stroke) => decodePoints(stroke.points, aspect));
    // Drawing types nothing: no word in the note that was not there before. (The
    // first edit of an older note writes its tags into the text as a hashtag line.)
    const words = (text) => new Set(text.match(/[\p{L}\p{N}#]+/gu) || []);
    const known = words(textBefore);
    const tagWords = new Set((saved.note.tags || []).flatMap((tag) => [`#${tag}`, tag]));
    out.newWords = [...words(saved.note.body)].filter((word) => !known.has(word) && !tagWords.has(word) && word !== "Sketch");
    out.textUnchanged = out.newWords.length === 0;
    out.saved = {
      strokes: saved.node.attrs.strokes.length,
      colors: saved.node.attrs.strokes.map((stroke) => stroke.color),
      aspect,
      hasSketches: saved.note.has_sketches,
      markdown: saved.note.body.includes("[Sketch]"),
      line: { points: line.length, x: span(line, 0), y: span(line, 1).map((v) => +(v / height).toFixed(3)) },
      loop: { points: loop.length, x: span(loop, 0), y: span(loop, 1).map((v) => +(v / height).toFixed(3)) },
      pressures: [...new Set([...line, ...loop].map((p) => p[2]))],
    };
    // Every sample the page was given is kept (the emulator does not deliver
    // every injected move; a real finger gives many more), and each lies on the
    // path the finger took, in the sketch's own units.
    const near = (value, target, slack) => Math.abs(value - target) <= slack;
    out.samplesGiven = (await evaluate("window.__sketchSamples")).slice(0, 2);
    out.allSamplesKept = out.samplesGiven[0] === line.length && out.samplesGiven[1] === loop.length;
    const onLine = ([x, y]) => x >= 1400 && x <= 8600 && near(y / height, 0.3 + ((x - 1500) / 7000) * 0.05, 0.02);
    const onLoop = ([x, y]) => near(((x - 5000) / 2000) ** 2 + ((y / height - 0.6) / 0.12) ** 2, 1, 0.3);
    out.strokesWhereDrawn =
      line.every(onLine) && near(line[0][0], 1500, 150) && near(line.at(-1)[0], 8500, 150) &&
      loop.every(onLoop) && near(loop[0][0], 7000, 150) && near(loop[0][1] / height, 0.6, 0.02);

    const preview = () => evaluate(`(() => {
      const figure = document.querySelector('.nw-prose .nw-sketch');
      if (!figure) return null;
      figure.scrollIntoView({ block: 'start' });
      const r = figure.getBoundingClientRect();
      const svg = figure.querySelector('svg');
      const paths = [...svg.querySelectorAll('path')];
      return {
        paths: paths.length,
        drawn: paths.every((p) => (p.getAttribute('d') || '').length > 10),
        viewBox: svg.getAttribute('viewBox'),
        ratio: +(r.height / r.width).toFixed(3),
        width: Math.round(r.width), height: Math.round(r.height), screenHeight: Math.round(visualViewport.height),
        sideScroll: document.scrollingElement.scrollWidth > innerWidth + 1,
      };
    })()`);
    // A tap on the visible top of the drawing (it can be taller than a turned screen).
    const tapSketch = async () => {
      const at = await evaluate(`(() => { const r = document.querySelector('.nw-prose .nw-sketch').getBoundingClientRect(); return { x: r.left + r.width / 2, y: Math.max(r.top, 0) + 40 }; })()`);
      await adb("shell", "input", "tap", ...(await toDevice(at.x, at.y)));
      await sleep(600);
    };
    out.previewPortrait = await preview();

    try {
      await adb("shell", "settings", "put", "system", "user_rotation", "1");
      await sleep(2500);
      await calibrate();
      out.previewTurned = await preview();
      await screenshot("sketch-turned");
      await tapSketch();
      if (!await waitUntil(areaReady, 10_000)) throw new Error("the sketch did not open on the turned screen");
      await sleep(1500);
      step("opened turned");
      area = await areaRect();
      out.turnedArea = { ...area, ratio: +(area.h / area.w).toFixed(3) };
      await trace([inArea(area, 0.1, 0.1), inArea(area, 0.3, 0.12), inArea(area, 0.5, 0.15)]);
      await screenshot("sketch-turned-editor");
      await tap(".nw-sketch-done");
      await waitUntil("!document.querySelector('.nw-sketch-editor')", 5000);
      saved = await savedWith(3);
      if (saved) {
        const added = decodePoints(saved.node.attrs.strokes[2].points, saved.node.attrs.aspect);
        out.turnedStroke = { aspectKept: saved.node.attrs.aspect === aspect, x: span(added, 0), y: span(added, 1).map((v) => +(v / height).toFixed(3)) };
      }
    } finally {
      await adb("shell", "settings", "put", "system", "user_rotation", "0");
      await sleep(2500);
      await calibrate();
    }
    out.previewBack = await preview();
    await screenshot("sketch-back");

    const sameShape = (shown) => shown && shown.drawn && near(shown.ratio, aspect, 0.02) && !shown.sideScroll;
    out.passed =
      out.keyboardWhileDrawing.covered < 80 && out.afterDone.keyboard.covered < 80 && out.textUnchanged && out.smallTargets.length === 0 && out.allSamplesKept &&
      new Set(out.swatches).size === 6 && !out.swatches.some((c) => /rgba\(0, 0, 0, 0\)|transparent/.test(c)) &&
      out.saved.colors.join() === "ink,pink" &&
      out.pointerTypes.length === 1 && out.pointerTypes[0] === "touch" &&
      out.afterUndo.undo && out.afterUndo.redo && out.afterRedo.undo && !out.afterRedo.redo &&
      out.saved.hasSketches && out.saved.markdown && out.strokesWhereDrawn &&
      sameShape(out.previewPortrait) && out.previewPortrait.paths === 2 &&
      sameShape(out.previewTurned) && out.previewTurned.paths === 2 &&
      near(out.turnedArea.ratio, aspect, 0.02) &&
      Boolean(out.turnedStroke?.aspectKept) && near(out.turnedStroke.x[0], 1000, 600) && near(out.turnedStroke.x[1], 5000, 600) &&
      sameShape(out.previewBack) && out.previewBack.paths === 3;
    return out;
  },

  /**
   * A locked note on the phone: locked with the profile password typed on the
   * on-screen keyboard, nothing of it left on screen after Lock Now, opened
   * again by touch, and locked by itself after the phone was away from Chrome
   * for longer than 15 minutes (the page's clock moved on while Chrome was in
   * the background, and the check runs on the way back).
   */
  async locking() {
    const out = (partial = {});
    const id = await openNoteTitled("Bench note 013");
    // A word only this note has, typed on the on-screen keyboard.
    const words = `gate${Date.now().toString(36)}`;
    await tapEndOfNote();
    await typeText(` ${words}`);
    if (!await waitUntil(`fetch("/api/notes/${id}").then((r) => r.json()).then((d) => d.note.body.includes(${JSON.stringify(words)}))`, 15_000)) throw new Error("the word never saved");
    out.words = words;
    await dismissKeyboard();
    step("note open");
    const paneShows = () => evaluate(`(document.querySelector('.nw-editorpane')?.innerText ?? '').includes(${JSON.stringify(words)})`);
    const typePassword = async () => {
      if (!await waitUntil("Boolean(document.querySelector('.nw-lock-dialog input[type=password]'))", 10_000)) throw new Error("no password prompt");
      await tap(".nw-lock-dialog input[type=password]");
      await typeText(profile.password);
      await sleep(400);
      out.keyboardWithPrompt = await keyboard();
      await dismissSavePassword();
      // Is the dialog's button in sight above the keyboard?
      out.promptButtonVisible = await evaluate(`(() => { const r = document.querySelector('.nw-lock-dialog button[type=submit]').getBoundingClientRect(); const vv = visualViewport; return r.top >= vv.offsetTop && r.bottom <= vv.offsetTop + vv.height; })()`);
      await screenshot("locking-prompt-keyboard");
      await tap(".nw-lock-dialog button[type=submit]");
    };
    // In the bar on a wide screen; in the note's "..." menu on a phone.
    if (await evaluate(`Boolean(document.querySelector('.nw-toolbar [aria-label="Lock Note"]')?.getClientRects().length)`)) {
      await tap('.nw-toolbar [aria-label="Lock Note"]');
    } else {
      await tap('.nw-toolbar [aria-label="More"]');
      await tapText(".nw-menu [role=menuitem]", "Lock Note");
    }
    out.prompt = await evaluate("document.querySelector('#nw-lock-message')?.textContent ?? null");
    await typePassword();
    const encrypted = `fetch("/api/notes/${id}").then((r) => r.json()).then((d) => d.note.content_format === "encrypted")`;
    if (!await waitUntil(encrypted, 60_000)) throw new Error("the note never locked");
    step("locked");
    const locked = await evaluate(`fetch("/api/notes/${id}").then((r) => r.json()).then((d) => d.note)`);
    out.server = { format: locked.content_format, body: locked.body, doc: locked.doc, preview: locked.preview };
    await dismissSavePassword();
    await screenshot("locking-locked-open");
    await dismissKeyboard();

    if (await evaluate(`Boolean(document.querySelector('.nw-toolbar [aria-label="Lock Now"]')?.getClientRects().length)`)) {
      await tap('.nw-toolbar [aria-label="Lock Now"]');
    } else {
      await tap('.nw-toolbar [aria-label="More"]');
      await tapText(".nw-menu [role=menuitem]", "Lock Now");
    }
    if (!await waitUntil("Boolean(document.querySelector('.nw-locked'))", 8000)) throw new Error("Lock Now didn't lock");
    out.paneAfterLockNow = await paneShows();
    await screenshot("locking-locked");
    step("lock now");

    const begun = Date.now();
    await tap(".nw-locked-open");
    await typePassword();
    out.opened = await waitUntil(`document.querySelector('.nw-prose')?.textContent.includes(${JSON.stringify(words)})`, 20_000);
    await dismissSavePassword();
    out.unlockMs = Date.now() - begun;
    await dismissKeyboard();
    step("opened");

    // Away from Chrome for "16 minutes": the page's clock moves on (a frozen
    // background page runs nothing, so it is moved first), Home at once, then back.
    await evaluate("(() => { const real = Date.now.bind(Date); Date.now = () => real() + 16 * 60 * 1000; return true; })()");
    await key(3);
    await sleep(2000);
    out.stillOpenWhileAway = await evaluate("!document.querySelector('.nw-locked')").catch(() => null);
    await adb("shell", "am", "start", "-n", "com.android.chrome/com.google.android.apps.chrome.Main");
    await sleep(2500);
    out.autoLocked = await waitUntil("Boolean(document.querySelector('.nw-locked'))", 8000);
    out.paneAfterAutoLock = await paneShows();
    await evaluate("location.reload(); true").catch(() => {});
    await sleep(3000);
    await waitUntil("Boolean(document.querySelector('.neo-mobile-bar, .system-nav'))", 20_000);
    await dismissSavePassword();
    await calibrate();
    step("auto-locked");

    // After a reload (as signing out and back in does), it opens locked: keys live only in the page.
    if (!await goTo("Notes")) throw new Error("Notes did not open after the reload");
    if (await evaluate("!document.querySelector('.nw-listpane')")) await backToList();
    await waitUntil(`Boolean(document.querySelector('.nw-row[data-note-id="${id}"]'))`, 15_000);
    await tap(`.nw-row[data-note-id="${id}"]`);
    out.lockedAfterReload = await waitUntil("Boolean(document.querySelector('.nw-locked'))", 10_000);
    out.paneAfterReload = await paneShows();
    step("reloaded");

    out.passed = out.server.format === "encrypted" && out.server.body === "" && out.server.doc === null &&
      !out.paneAfterLockNow && out.opened && out.autoLocked && !out.paneAfterAutoLock && out.lockedAfterReload && !out.paneAfterReload;
    return out;
  },

  /** Names for every control, touch-sized targets, and text contrast, on the Notes screens. */
  async accessibility() {
    const out = {};
    await openNoteTitled("Bench note 006");
    const audit = () => evaluate(`(() => {
      const visible = (el) => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
      const controls = [...document.querySelectorAll('.nw button, .nw [role=button], .nw input, .nw [role=menuitem], .neo-mobile-bar button')].filter(visible);
      const unnamed = controls.filter((el) => !(el.getAttribute('aria-label') || el.getAttribute('title') || el.textContent.trim() || el.labels?.[0]?.textContent.trim() || el.getAttribute('placeholder')));
      // A field's target is the whole labelled field, which focuses its input.
      const target = (el) => (el.tagName === 'INPUT' && el.closest('label')) || el;
      const small = controls.filter((el) => { const r = target(el).getBoundingClientRect(); return el.closest('.nw-prose') === null && (r.width < 44 || r.height < 44); })
        .map((el) => { const r = target(el).getBoundingClientRect(); return (el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.className || el.tagName) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height); });
      // rgb(0-255) or color(srgb 0-1); alpha last when present.
      const parse = (c) => {
        const n = (c.match(/[\\d.]+/g) || []).map(Number);
        return c.startsWith('color(') ? [n[0] * 255, n[1] * 255, n[2] * 255, n[3] ?? 1] : n;
      };
      const lum = ([r, g, b]) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
      const bgOf = (el) => { for (let e = el; e; e = e.parentElement) { const c = parse(getComputedStyle(e).backgroundColor); if (c.length >= 3 && (c[3] ?? 1) > 0.5) return c; } return [0, 0, 0]; };
      const texts = [...document.querySelectorAll('.nw .nw-row-title, .nw .nw-row-line time, .nw .nw-row-preview, .nw-prose p, .nw-date, .neo-mobile-title')].filter(visible).slice(0, 40);
      const lowContrast = texts.map((el) => { const fg = lum(parse(getComputedStyle(el).color)); const bg = lum(bgOf(el)); const ratio = (Math.max(fg, bg) + 0.05) / (Math.min(fg, bg) + 0.05); return [el.className || el.tagName, Math.round(ratio * 10) / 10]; }).filter(([, r]) => r < 4.5);
      return { controls: controls.length, unnamed: unnamed.map((el) => el.outerHTML.slice(0, 80)), small: [...new Set(small)], lowContrast };
    })()`);
    out.note = await audit();
    await backToList();
    out.list = await audit();
    // The folders: their own screen on a phone, beside the list elsewhere.
    if (await evaluate("Boolean(document.querySelector('.nw-listpane .nw-back')?.getClientRects().length)")) {
      await tap(".nw-listpane .nw-back");
      await waitUntil("Boolean(document.querySelector('.nw-sidebar'))", 4000);
    }
    out.folders = await audit();
    const views = [out.note, out.list, out.folders];
    out.passed = views.every((v) => !v.unnamed.length && !v.small.length && !v.lowContrast.length);
    return out;
  },

  /** What a person waits for, measured in Chrome on the emulator: opening a note, and typing. */
  async performance() {
    const out = {};
    await goTo("Notes");
    await backToList();
    await waitUntil("document.querySelectorAll('.nw-row[data-note-id]').length >= 5", 10_000);
    await evaluate(`window.__perf = { events: [], long: [] };
      new PerformanceObserver((l) => { for (const e of l.getEntries()) __perf.events.push([e.name, e.duration]); }).observe({ type: 'event', buffered: false, durationThreshold: 16 });
      new PerformanceObserver((l) => { for (const e of l.getEntries()) __perf.long.push(e.duration); }).observe({ type: 'longtask', buffered: false }); true`);
    const opens = [];
    for (const title of ["Bench note 007", "Bench note 008", "Bench note 009"]) {
      const index = await evaluate(`[...document.querySelectorAll('.nw-row[data-note-id]')].findIndex((r) => r.querySelector('.nw-row-title')?.textContent.trim() === ${JSON.stringify(title)})`);
      if (index < 0) continue;
      await evaluate(`window.__shown = null; window.__tapped = null; addEventListener('pointerup', () => { __tapped = performance.now(); }, { capture: true, once: true });
        (function watch() { if (document.querySelector('.nw-prose h1')?.textContent.trim() === ${JSON.stringify(title)}) { __shown = performance.now(); return; } requestAnimationFrame(watch); })(); true`);
      await tap(".nw-row[data-note-id]", index);
      await waitUntil("window.__shown !== null", 8000);
      const ms = await evaluate("window.__shown && window.__tapped ? Math.round(__shown - __tapped) : null");
      opens.push({ title, tapToContentMs: ms });
      await backToList();
    }
    out.opens = opens;
    // Typing at a person's pace, one key at a time through the keyboard, on a
    // short note and on the 20 KB one.
    out.typing = {};
    for (const title of ["Bench note 010", "Long bench note"]) {
      await openNoteTitled(title);
      await tapEndOfNote();
      await waitUntil("document.querySelector('.nw')?.dataset.keyboard === 'open'", 6000);
      await key(66);
      await sleep(500);
      await evaluate("__perf.events.length = 0; __perf.long.length = 0; true");
      const profiling = process.env.MOBILE_PROFILE && title === "Long bench note";
      if (profiling) {
        // What each piece of a snapshot costs on this device, on this note.
        out.snapshotCostsMs = await evaluate(`(() => {
          const editor = document.querySelector('.nw-prose')?.editor;
          if (!editor) return null;
          const time = (fn, n = 10) => { fn(); const t = performance.now(); for (let i = 0; i < n; i++) fn(); return +((performance.now() - t) / n).toFixed(1); };
          const json = editor.state.doc.toJSON();
          const text = JSON.stringify(json);
          return {
            docChars: editor.state.doc.textContent.length, jsonBytes: text.length,
            toJSON: time(() => editor.state.doc.toJSON()), stringify: time(() => JSON.stringify(json)),
            structuredClone: time(() => structuredClone(json)), cloneString: time(() => structuredClone(text)),
            markdown: time(() => editor.markdown.serialize(json), 5),
          };
        })()`);
        await cdp.send("Performance.enable");
        out.metricsBefore = Object.fromEntries((await cdp.send("Performance.getMetrics")).metrics.map((m) => [m.name, m.value]));
        await cdp.send("Profiler.enable");
        await cdp.send("Profiler.setSamplingInterval", { interval: 200 });
        await cdp.send("Profiler.start");
      }
      const text = "the quick brown fox jumps over";
      for (const char of text) {
        if (char === " ") await key(62);
        else await adb("shell", "input", "text", char);
        await sleep(60);
      }
      await sleep(2000);
      if (profiling) {
        const { profile: cpu } = await cdp.send("Profiler.stop");
        const after = Object.fromEntries((await cdp.send("Performance.getMetrics")).metrics.map((m) => [m.name, m.value]));
        const delta = (name) => +(after[name] - out.metricsBefore[name]).toFixed(3);
        out.renderingDuringTyping = {
          recalcStyleCount: delta("RecalcStyleCount"), recalcStyleSeconds: delta("RecalcStyleDuration"),
          layoutCount: delta("LayoutCount"), layoutSeconds: delta("LayoutDuration"),
          scriptSeconds: delta("ScriptDuration"), taskSeconds: delta("TaskDuration"),
          nodes: after.Nodes,
        };
        delete out.metricsBefore;
        await writeFile(join(OUT, "typing-long-note.cpuprofile"), JSON.stringify(cpu));
        // Self time by function, the heaviest first.
        const byId = new Map(cpu.nodes.map((n) => [n.id, n]));
        const self = new Map();
        const gaps = cpu.timeDeltas;
        cpu.samples.forEach((id, i) => {
          const frame = byId.get(id).callFrame;
          const name = `${frame.functionName || "(anonymous)"} ${frame.url.split("/").pop()}:${frame.lineNumber + 1}`;
          self.set(name, (self.get(name) || 0) + (gaps[i] || 0) / 1000);
        });
        out.profileTop = [...self.entries()].filter(([n]) => !n.startsWith("(idle)")).sort((a, b) => b[1] - a[1]).slice(0, 15).map(([n, ms]) => [n, Math.round(ms)]);
      }
      const perf = await evaluate("__perf");
      const keyEvents = perf.events.filter(([name]) => ['keydown', 'keyup', 'keypress', 'beforeinput', 'input'].includes(name)).map(([, d]) => d).sort((a, b) => a - b);
      out.typing[title] = {
        keys: text.length,
        eventsOver16ms: keyEvents.length,
        p95Ms: keyEvents.length ? keyEvents[Math.floor(keyEvents.length * 0.95)] : 0,
        maxMs: keyEvents.at(-1) ?? 0,
        longTasks: perf.long.length,
        longTasksOver50: perf.long.filter((d) => d > 50).length,
        longTaskMaxMs: Math.round(Math.max(0, ...perf.long)),
      };
      await dismissKeyboard();
    }
    out.note = "Emulator timing: an ARM64 image with a software GPU on an Apple M5. Not a real phone.";
    out.passed = out.opens.every((o) => o.tapToContentMs !== null) && Object.values(out.typing).every((t) => t.longTasksOver50 === 0);
    return out;
  },
};

/* --------------------------------------------------------------- main -- */

try {
  await mkdir(OUT, { recursive: true });
  await cleanUpDeadRun();
  await bootDevice();
  await adb("shell", "settings", "put", "system", "accelerometer_rotation", "0");
  await adb("shell", "settings", "put", "system", "user_rotation", "0");
  await openChrome(ORIGIN, { fresh: true });
  await createProfile();
  await calibrate();
  for (const name of CHECKS) {
    const begun = Date.now();
    trail = [];
    partial = null;
    try {
      report.checks[name] = await checks[name]();
    } catch (error) {
      report.checks[name] = { ...partial, passed: false, error: String(error?.message || error), reached: trail.slice() };
      await screenshot(`failed-${name}`).catch(() => {});
    }
    report.checks[name].seconds = Math.round((Date.now() - begun) / 1000);
    console.error(`  ${name}: ${report.checks[name].passed ? "passed" : "FAILED"}`);
  }
  report.pageErrors = await evaluate("window.__errors || []").catch(() => null);
} catch (error) {
  report.failure = String(error?.stack || error);
} finally {
  await deleteProfile().catch((error) => {
    report.profileDeleted = false;
    report.notes.push(`profile cleanup failed: ${error.message}`);
  });
  cdp?.close();
  await writeFile(join(OUT, "report.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ out: OUT, device: report.device, checks: Object.fromEntries(Object.entries(report.checks).map(([k, v]) => [k, v.passed])), failure: report.failure, profileDeleted: report.profileDeleted }, null, 2));
}
