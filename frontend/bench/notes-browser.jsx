// Notes browser suite: what Node cannot check, in each real browser.
//
// 1. Fidelity: every fixture goes through the browser's own HTML parsing (how
//    Markdown with HTML is read in a browser), into a real editor and back out
//    as Markdown. Each must keep its meaning, or be flagged as lossy -- never
//    change silently.
// 2. Journal: the recovery journal on real IndexedDB -- two "tabs" writing at
//    once, compare-and-delete, persistence across reopening, Web Locks.
//
// 3. Chords: every Notes chord through Neo's real keyboard engine and keymap
//    into a real editor, in each form a browser may report it (on a Mac,
//    Cmd+Option+1 can arrive as "¡" or "1", Cmd+Shift+7 as "&" or "7"), and
//    the editor's own list keys. Synthetic events: this checks what the page
//    does with a chord, not whether the browser hands it over.
//
// 4. Attachments and links: pasting and dropping files, pictures made smaller
//    in this browser's own canvas, ">>" links. The upload is a stand-in (no
//    server or sign-in here); the real one is tested on a production build.
//
// 5. Sketches: the drawing screen itself, with pointer events as a mouse, a
//    pen (with pressure) and a finger send them, drawn on this browser's
//    canvas, then shown in a note. Synthetic events: real finger and pen
//    input is tested on the Android Emulator.
//
// 6. Locking: this browser's WebCrypto against the values the server's Python
//    produces (tests/fixtures/notes_key_vectors.json, served by the runner),
//    and what it costs here to derive a key at the real 600,000 rounds.
//
// Results land on window.__suite and, given ?report=<url>, are POSTed there.
import { Editor } from "@tiptap/core";
import { TextSelection } from "@tiptap/pm/state";
import { createElement } from "react";
import { flushSync } from "react-dom";
import { createRoot } from "react-dom/client";

import "../src/index.css";
import "../src/notes/notes.css";
import SketchEditor from "../src/notes/SketchEditor.jsx";
import { decodePoints } from "../src/notes/sketch/strokes.js";
import {
  deriveKekBytes,
  encryptWith,
  fromBase64,
  lockingAvailable,
  openContent,
  openFile,
  openWrap,
  sealContent,
  toBase64,
} from "../src/notes/crypto.js";

import { COMMANDS } from "../src/keys/commands.js";
import { armEngine } from "../src/keys/engine.js";
import { buildKeymap } from "../src/keys/keymap.js";
import { registerCommandHandlers } from "../src/keys/registry.js";
import { preparePicture } from "../src/notes/extensions/attachments.js";
import { noteCommandHandlers } from "../src/notes/noteCommands.js";

import { compareMarkdown } from "../src/notes/fingerprint.js";
import { noteExtensions } from "../src/notes/noteSchema.js";
import { markdownManager, noteToDoc, projectNote } from "../src/notes/projection.js";
import {
  createJournal,
  deleteJournal,
  holdTabLock,
  indexedDbJournalAdapter,
  liveTabIds,
} from "../src/notes/recoveryJournal.js";
import { HTML, MARKDOWN } from "./notes-fixtures.js";

const results = [];
/** `pass` null: this browser can't be tested this way (said why), not a pass. */
function record(suite, name, pass, detail = {}) {
  results.push({ suite, name, pass: pass === null ? null : Boolean(pass), detail });
}

function editorRoundTrip(doc) {
  const host = document.createElement("div");
  document.body.append(host);
  const editor = new Editor({ element: host, extensions: noteExtensions(), content: doc });
  const json = editor.getJSON();
  editor.destroy();
  host.remove();
  return json;
}

function fidelity() {
  const manager = markdownManager();
  for (const [name, markdown] of Object.entries(MARKDOWN)) {
    const { doc, lossy } = noteToDoc({ title: "Fixture", body: markdown, tags: [] }, manager);
    const saved = projectNote(editorRoundTrip(doc), manager);
    const same = compareMarkdown(markdown, saved.body).equal;
    record("fidelity", name, !lossy && same && saved.title === "Fixture", { lossy, same, body: same ? undefined : saved.body });
  }
  for (const [name, markdown] of Object.entries(HTML)) {
    const { doc, lossy, reasons } = noteToDoc({ title: "Fixture", body: markdown, tags: [] }, manager);
    const saved = projectNote(editorRoundTrip(doc), manager);
    const same = compareMarkdown(markdown, saved.body).equal;
    // Either it survives, or the person is told before anything can change.
    record("fidelity-html", name, same || lossy, { lossy, same, reasons, body: saved.body });
  }
}

async function journal() {
  const profile = `suite-${crypto.randomUUID()}`;
  const first = await indexedDbJournalAdapter(profile);
  const second = await indexedDbJournalAdapter(profile);
  record("journal", "indexeddb opens", Boolean(first && second));
  if (!first || !second) return;
  const tabA = createJournal(first, { tabId: "tab-a" });
  const tabB = createJournal(second, { tabId: "tab-b" });

  const writes = [];
  for (let i = 0; i < 25; i += 1) {
    writes.push(tabA.write("note-1", { snapshot: { body: `a${i}` } }));
    writes.push(tabB.write("note-1", { snapshot: { body: `b${i}` } }));
  }
  const seqs = await Promise.all(writes);
  const unique = new Set(seqs).size === seqs.length;
  const max = Math.max(...seqs);
  record("journal", "two tabs get unique, rising numbers", unique && max === 50, { max, unique });

  const slots = (await tabA.entries()).filter((entry) => entry.noteKey === "note-1");
  record("journal", "each tab keeps its own slot", slots.length === 2, { slots: slots.map((s) => [s.tabId, s.seq]) });

  const mine = slots.find((slot) => slot.tabId === "tab-a");
  const removedOld = await tabA.ack("note-1", mine.seq - 1);
  const stillThere = (await tabA.entries()).some((entry) => entry.tabId === "tab-a");
  record("journal", "a stale acknowledgement removes nothing", !removedOld && stillThere);
  const removed = await tabA.ack("note-1", mine.seq);
  const gone = !(await tabA.entries()).some((entry) => entry.tabId === "tab-a");
  record("journal", "the matching acknowledgement removes the slot", removed && gone);

  const seq = await tabA.write("note-2", { snapshot: { body: "kept" } }, { strict: true });
  await first.close();
  const reopened = await indexedDbJournalAdapter(profile);
  const survived = (await reopened.list()).find((entry) => entry.noteKey === "note-2");
  record("journal", "entries survive closing and reopening", survived?.seq === seq && survived?.snapshot.body === "kept");

  const release = holdTabLock("tab-live");
  const releaseB = holdTabLock("tab-b");
  await new Promise((resolve) => setTimeout(resolve, 50));
  const live = await liveTabIds();
  if (live) {
    const tabLive = createJournal(reopened, { tabId: "tab-live" });
    await createJournal(reopened, { tabId: "tab-gone" }).write("note-3", { snapshot: {} });
    const recoverable = (await tabLive.recoverable(live)).map((entry) => entry.tabId);
    // tab-b holds its lock (alive): its slot is not replayed; tab-gone holds none.
    record("journal", "web locks tell live tabs from gone ones", live.has("tab-live") && recoverable.includes("tab-gone") && !recoverable.includes("tab-b"), {
      live: [...live],
      recoverable,
    });
  } else {
    record("journal", "web locks tell live tabs from gone ones", false, { reason: "navigator.locks.query unavailable" });
  }
  release();
  releaseB();
  await reopened.close();
  await second.close();
  record("journal", "the journal can be deleted with its profile", await deleteJournal(profile));
}

const paragraph = (text) => ({ type: "paragraph", content: [{ type: "text", text }] });
const item = (text, type = "listItem", attrs) => ({ type, ...(attrs ? { attrs } : {}), content: [paragraph(text)] });
const TWO_ITEMS = { type: "doc", content: [{ type: "bulletList", content: [item("One"), item("Two")] }] };
const TWO_TASKS = { type: "doc", content: [{ type: "taskList", content: [item("One", "taskItem", { checked: false }), item("Two", "taskItem", { checked: false })] }] };
const ONE_LINE = { type: "doc", content: [paragraph("Alpha")] };

function chords() {
  const host = document.createElement("div");
  document.body.append(host);
  const editor = new Editor({ element: host, extensions: noteExtensions(), content: ONE_LINE });
  const unregister = registerCommandHandlers(noteCommandHandlers(() => editor));
  let keymap = buildKeymap(COMMANDS, [], { platform: "mac" });
  const disarm = armEngine({ getKeymap: () => keymap, getContext: () => ({ scopes: new Set(["notes"]) }) });

  const first = (doc) => doc.content[0];
  /** Load `doc`, put the caret in the text block containing `text`, press, report. */
  function press({ doc = ONE_LINE, at = "Alpha", key, code, mods, keyCode, select = false }) {
    editor.commands.setContent(doc);
    let pos = null;
    let end = null;
    editor.state.doc.descendants((node, position) => {
      if (pos === null && node.isText && node.text.includes(at)) {
        pos = position + 1;
        end = position + node.text.length;
      }
    });
    editor.view.dispatch(editor.state.tr.setSelection(TextSelection.create(editor.state.doc, select ? pos - 1 : pos, select ? end : pos)));
    editor.view.focus();
    const event = new KeyboardEvent("keydown", { key, code, bubbles: true, cancelable: true, ...mods });
    // Real keystrokes carry a key code; ProseMirror's Caps Lock fallback reads it.
    if (keyCode) Object.defineProperty(event, "keyCode", { get: () => keyCode });
    editor.view.dom.dispatchEvent(event);
    return editor.state.doc.toJSON();
  }
  const marked = (type) => (json) => JSON.stringify(json).includes(`{"type":"${type}"}`);
  const isBlock = (type, attrs = {}) => (json) => {
    const block = json.content[0];
    return block.type === type && Object.entries(attrs).every(([k, v]) => block.attrs?.[k] === v);
  };
  const cases = [
    ["notes.styleTitle", ["¡", "1"], "Digit1", { alt: true }, {}, isBlock("heading", { level: 1 })],
    ["notes.styleHeading", ["™", "2"], "Digit2", { alt: true }, {}, isBlock("heading", { level: 2 })],
    ["notes.styleSubheading", ["£", "3"], "Digit3", { alt: true }, {}, isBlock("heading", { level: 3 })],
    ["notes.styleBody", ["º", "0"], "Digit0", { alt: true }, { doc: { type: "doc", content: [{ type: "heading", attrs: { level: 2 }, content: [{ type: "text", text: "Alpha" }] }] } }, isBlock("paragraph")],
    ["notes.styleMono", ["M", "m"], "KeyM", { shift: true }, {}, isBlock("codeBlock")],
    ["notes.bulletedList", ["&", "7"], "Digit7", { shift: true }, {}, isBlock("bulletList", { variant: "bullet" })],
    ["notes.dashedList", ["*", "8"], "Digit8", { shift: true }, {}, isBlock("bulletList", { variant: "dash" })],
    ["notes.numberedList", ["(", "9"], "Digit9", { shift: true }, {}, isBlock("orderedList")],
    ["notes.checklist", ["L", "l"], "KeyL", { shift: true }, {}, isBlock("taskList")],
    ["notes.markChecked", ["U", "u"], "KeyU", { shift: true }, { doc: TWO_TASKS, at: "One" }, (json) => first(json).content[0].attrs.checked === true],
    ["notes.insertTable", ["†", "t"], "KeyT", { alt: true }, {}, (json) => json.content.some((block) => block.type === "table")],
  ];
  for (const platform of ["mac", "windows"]) {
    keymap = buildKeymap(COMMANDS, [], { platform });
    for (const [id, keys, code, extra, setup, check] of cases) {
      // Windows reports the plain key with Ctrl; a Mac may report either form with Cmd.
      for (const key of platform === "mac" ? keys : [keys[1]]) {
        const mods = { metaKey: platform === "mac", ctrlKey: platform !== "mac", altKey: Boolean(extra.alt), shiftKey: Boolean(extra.shift) };
        const json = press({ ...setup, key, code, mods });
        record("chords", `${platform} ${id} as ${JSON.stringify(key)}`, check(json), { got: JSON.stringify(json).slice(0, 160) });
      }
    }
  }
  // The editor's own keys (the same on both platforms apart from Mod).
  keymap = buildKeymap(COMMANDS, [], { platform: "mac" });
  const nested = (json) => first(json).content.length === 1 && first(json).content[0].content.some((node) => node.type === "bulletList");
  const order = (json) => first(json).content.map((li) => li.content[0].content[0].text).join(",");
  const local = [
    ["Cmd+B bolds", { select: true, key: "b", code: "KeyB", keyCode: 66, mods: { metaKey: true } }, marked("bold")],
    ["Cmd+B with Caps Lock on bolds", { select: true, key: "B", code: "KeyB", keyCode: 66, mods: { metaKey: true } }, marked("bold")],
    ["Cmd+U underlines", { select: true, key: "u", code: "KeyU", keyCode: 85, mods: { metaKey: true } }, marked("underline")],
    ["Cmd+Shift+U checks the item and does not underline", { doc: TWO_TASKS, at: "One", select: true, key: "U", code: "KeyU", keyCode: 85, mods: { metaKey: true, shiftKey: true } }, (json) => first(json).content[0].attrs.checked === true && !marked("underline")(json)],
    ["Tab nests a list item", { doc: TWO_ITEMS, at: "Two", key: "Tab", code: "Tab", mods: {} }, nested],
    ["Cmd+] nests a list item", { doc: TWO_ITEMS, at: "Two", key: "]", code: "BracketRight", mods: { metaKey: true } }, nested],
    ["Ctrl+Cmd+Down moves an item down", { doc: TWO_ITEMS, at: "One", key: "ArrowDown", code: "ArrowDown", mods: { metaKey: true, ctrlKey: true } }, (json) => order(json) === "Two,One"],
    ["Alt+Shift+Up moves an item up", { doc: TWO_ITEMS, at: "Two", key: "ArrowUp", code: "ArrowUp", mods: { altKey: true, shiftKey: true } }, (json) => order(json) === "Two,One"],
  ];
  for (const [name, args, check] of local) {
    const json = press(args);
    record("chords", name, check(json), { got: JSON.stringify(json).slice(0, 160) });
  }
  // Redo is Cmd+Shift+Z whichever case the browser reports the Z in.
  for (const key of ["z", "Z"]) {
    press({ select: true, key: "b", code: "KeyB", keyCode: 66, mods: { metaKey: true } });
    const send = (k, mods, code) => {
      const event = new KeyboardEvent("keydown", { key: k, code, bubbles: true, cancelable: true, ...mods });
      Object.defineProperty(event, "keyCode", { get: () => code.charCodeAt(3) });
      editor.view.dom.dispatchEvent(event);
    };
    send("z", { metaKey: true }, "KeyZ");
    const undone = !marked("bold")(editor.state.doc.toJSON());
    send(key, { metaKey: true, shiftKey: true }, "KeyZ");
    record("chords", `Cmd+Shift+Z redoes as ${JSON.stringify(key)}`, undone && marked("bold")(editor.state.doc.toJSON()));
  }
  // Shift+Tab and Cmd+[ undo a nesting.
  for (const [name, key, code, mods] of [["Shift+Tab lifts it back", "Tab", "Tab", { shiftKey: true }], ["Cmd+[ lifts it back", "[", "BracketLeft", { metaKey: true }]]) {
    press({ doc: TWO_ITEMS, at: "Two", key: "Tab", code: "Tab", mods: {} });
    const event = new KeyboardEvent("keydown", { key, code, bubbles: true, cancelable: true, ...mods });
    editor.view.dom.dispatchEvent(event);
    record("chords", name, first(editor.state.doc.toJSON()).content.length === 2, { got: JSON.stringify(editor.state.doc.toJSON()).slice(0, 160) });
  }
  disarm();
  unregister();
  editor.destroy();
  host.remove();
}

async function attachments() {
  const host = document.createElement("div");
  document.body.append(host);
  let uploads = 0;
  const upload = async (file) => {
    uploads += 1;
    const id = `0000000${uploads}-1111-4222-8333-944455556666`.slice(-36);
    const image = file.type.startsWith("image/");
    return { attachment: { id, kind: image ? "image" : "file", name: file.name, size: file.size, mime_type: file.type, url: `/api/files/${id}/download` } };
  };
  const notes = [{ id: "a1b2c3d4-0000-4000-8000-000000000001", title: "Budget" }, { id: "a1b2c3d4-0000-4000-8000-000000000002", title: "Budget draft" }];
  const editor = new Editor({
    element: host,
    extensions: noteExtensions({
      upload,
      noteLinks: { search: async (q) => notes.filter((n) => n.title.toLowerCase().includes(q.toLowerCase())), titleOf: () => null, subscribe: () => () => {}, onOpen: () => {} },
    }),
    content: { type: "doc", content: [{ type: "heading", attrs: { level: 1 }, content: [{ type: "text", text: "Trip" }] }, { type: "paragraph" }] },
  });
  const until = async (check, ms = 4000) => {
    const begun = performance.now();
    while (performance.now() - begun < ms) {
      if (check()) return true;
      await new Promise((resolve) => setTimeout(resolve, 30));
    }
    return false;
  };
  const types = () => JSON.stringify(editor.getJSON());
  editor.commands.focus("end");

  // A picture, pasted.
  const png = await new Promise((resolve) => {
    const canvas = document.createElement("canvas");
    canvas.width = 40;
    canvas.height = 30;
    canvas.getContext("2d").fillRect(0, 0, 40, 30);
    canvas.toBlob(resolve, "image/png");
  });
  let pasteEvent;
  try {
    const paste = new DataTransfer();
    paste.items.add(new File([png], "pasted.png", { type: "image/png" }));
    pasteEvent = new ClipboardEvent("paste", { clipboardData: paste, bubbles: true, cancelable: true });
  } catch (error) {
    pasteEvent = null;
    record("attachments", "a pasted picture is added", false, { reason: `this browser cannot build a paste event with files: ${error.message}` });
  }
  if (pasteEvent) {
    if (!pasteEvent.clipboardData?.files?.length) {
      record("attachments", "a pasted picture is added", null, { reason: "this browser gives a script-made paste event no files; only a real paste can test it" });
    } else {
      editor.view.dom.dispatchEvent(pasteEvent);
      record("attachments", "a pasted picture is added", await until(() => types().includes('"type":"image"')), { doc: types().slice(0, 200) });
    }
  }

  // A file, dropped below the title.
  const drop = new DataTransfer();
  drop.items.add(new File(["hello"], "notes.txt", { type: "text/plain" }));
  const below = editor.view.dom.querySelector("h1").getBoundingClientRect();
  editor.view.dom.dispatchEvent(new DragEvent("drop", { dataTransfer: drop, clientX: below.left + 10, clientY: below.bottom + 4, bubbles: true, cancelable: true }));
  record("attachments", "a dropped file becomes a file chip", await until(() => types().includes('"type":"fileAttachment"')), { doc: types().slice(0, 200) });

  // A large picture is made smaller, the right way up, in this browser's canvas.
  const big = await new Promise((resolve) => {
    const canvas = document.createElement("canvas");
    canvas.width = 3600;
    canvas.height = 2400;
    const context = canvas.getContext("2d");
    context.fillStyle = "#3a7";
    context.fillRect(0, 0, 3600, 2400);
    canvas.toBlob(resolve, "image/jpeg", 0.95);
  });
  const prepared = await preparePicture(new File([big], "big.jpg", { type: "image/jpeg" }), 5 * 1024 * 1024);
  const bitmap = await createImageBitmap(prepared);
  record("attachments", "a large picture is made at most 2560 px", Math.max(bitmap.width, bitmap.height) === 2560, { size: [bitmap.width, bitmap.height], bytes: prepared.size });

  // ">>" and a few letters list notes; Enter links the first.
  editor.commands.focus("end");
  editor.commands.insertContent(" see >>Budg");
  const listed = await until(() => document.querySelectorAll(".nw-suggest-item").length === 2);
  record("links", "typing >> lists matching notes", listed, { items: [...document.querySelectorAll(".nw-suggest-item")].map((item) => item.textContent) });
  editor.view.dom.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
  record("links", "Enter links the note", await until(() => types().includes('"type":"noteLink"')), { doc: types().slice(-200) });

  const markdown = editor.markdown.serialize(editor.getJSON());
  const pasted = types().includes('"type":"image"');
  record("attachments", "the Markdown mirror carries the picture, the file and the link", (!pasted || /!\[pasted\.png\]\(\/api\/files\//.test(markdown)) && /\[notes\.txt\]\(\/api\/files\//.test(markdown) && /\[Budget\]\(note:\/\//.test(markdown), { markdown, pictureWasPasted: pasted });
  editor.destroy();
  host.remove();
}

async function sketches() {
  const frame = () => new Promise((resolve) => requestAnimationFrame(() => setTimeout(resolve, 0)));
  const host = document.createElement("div");
  document.body.append(host);
  let finished = null;
  const root = createRoot(host);
  flushSync(() => root.render(createElement(SketchEditor, { initial: { aspect: 0.75, strokes: [] }, isNew: true, onDone: (sketch) => (finished = sketch), onCancel: () => {} })));
  for (let i = 0; i < 5; i += 1) await frame();
  const input = document.querySelector(".nw-sketch-input");
  const rect = input.getBoundingClientRect();
  const at = (fx, fy) => [rect.left + rect.width * fx, rect.top + rect.height * fy];
  let pointerId = 10;
  const stroke = async (pointerType, points, pressures = []) => {
    pointerId += 1;
    const send = (type, [x, y], pressure) =>
      input.dispatchEvent(new PointerEvent(type, { pointerId, pointerType, isPrimary: true, clientX: x, clientY: y, pressure, buttons: type === "pointerup" ? 0 : 1, button: type === "pointermove" ? -1 : 0, bubbles: true, cancelable: true, composed: true }));
    send("pointerdown", points[0], pressures[0] ?? 0.5);
    for (let i = 1; i < points.length; i += 1) send("pointermove", points[i], pressures[i] ?? 0.5);
    send("pointerup", points.at(-1), 0);
    await frame();
  };
  const press = async (selector) => {
    document.querySelector(selector).click();
    await frame();
  };
  const done = async () => {
    finished = null;
    await press(".nw-sketch-done");
    return finished;
  };
  const spread = (from, to, count) => Array.from({ length: count }, (_, i) => [from[0] + ((to[0] - from[0]) * i) / (count - 1), from[1] + ((to[1] - from[1]) * i) / (count - 1)]);

  await stroke("mouse", spread(at(0.2, 0.5), at(0.8, 0.5), 12));
  let sketch = await done();
  const aspect = sketch?.aspect ?? 0.75;
  const points = sketch?.strokes?.[0] ? decodePoints(sketch.strokes[0].points, aspect) : [];
  const xs = points.map((p) => p[0]);
  const ys = points.map((p) => p[1] / (10000 * aspect));
  record("sketches", "a mouse stroke is kept where it went", points.length === 12 && Math.abs(Math.min(...xs) - 2000) < 60 && Math.abs(Math.max(...xs) - 8000) < 60 && ys.every((y) => Math.abs(y - 0.5) < 0.01), { count: points.length, x: [Math.min(...xs), Math.max(...xs)] });
  // Painted on this browser's canvas, in the ink colour of the theme.
  const base = document.querySelector(".nw-sketch-canvas");
  const scale = base.width / rect.width;
  const read = (fx, fy) => [...base.getContext("2d").getImageData(Math.round(rect.width * fx * scale), Math.round(rect.height * fy * scale), 1, 1).data];
  const pixel = read(0.5, 0.5);
  const blank = read(0.5, 0.85);
  // Against a blank spot, not an exact value: Brave adds noise to what a page reads back from a canvas.
  record("sketches", "the stroke is painted on the canvas", pixel[3] - blank[3] > 128, { pixel, blank });

  await stroke("pen", spread(at(0.2, 0.25), at(0.8, 0.25), 10), Array.from({ length: 10 }, (_, i) => 0.2 + i * 0.08));
  sketch = await done();
  const pen = sketch?.strokes?.[1] ? decodePoints(sketch.strokes[1].points, aspect).map((p) => p[2]) : [];
  record("sketches", "a pen's pressure is kept", pen.length === 10 && Math.min(...pen) <= 0.2 && Math.max(...pen) >= 0.9, { pressures: [Math.min(...pen), Math.max(...pen)] });

  await stroke("touch", spread(at(0.2, 0.8), at(0.8, 0.8), 6));
  sketch = await done();
  record("sketches", "a touch while a pen is in use is not drawn (a resting palm)", sketch?.strokes?.length === 2, { strokes: sketch?.strokes?.length });

  // The eraser, in one long step across the mouse line only.
  [...document.querySelectorAll(".nw-sketch-tool")].find((b) => b.textContent === "Eraser").click();
  await frame();
  await stroke("mouse", [at(0.5, 0.4), at(0.5, 0.6)]);
  sketch = await done();
  record("sketches", "the eraser takes a line it crosses between two samples, and only that", sketch?.strokes?.length === 1 && decodePoints(sketch.strokes[0].points, aspect)[0][2] !== 0.5, { strokes: sketch?.strokes?.length });
  await press('.nw-sketch-editor [aria-label="Undo"]');
  const undone = (await done())?.strokes?.length;
  await press('.nw-sketch-editor [aria-label="Redo"]');
  const redone = (await done())?.strokes?.length;
  record("sketches", "undo brings the erased line back, redo takes it again", undone === 2 && redone === 1, { undone, redone });

  const swatches = [...document.querySelectorAll(".nw-sketch-color")].map((b) => getComputedStyle(b, "::before").backgroundColor);
  record("sketches", "every colour has a visible swatch", new Set(swatches).size === 6 && !swatches.some((c) => /rgba\(0, 0, 0, 0\)|transparent/.test(c)), { swatches });

  // Shown in a note, at the note's width, the same shape.
  [...document.querySelectorAll(".nw-sketch-tool")].find((b) => b.textContent === "Pen").click();
  await frame();
  await stroke("pen", spread(at(0.3, 0.7), at(0.6, 0.7), 5), [0.5, 0.6, 0.7, 0.6, 0.5]);
  sketch = await done();
  flushSync(() => root.unmount());
  host.remove();
  const noteHost = document.createElement("div");
  noteHost.style.width = "480px";
  document.body.append(noteHost);
  const editor = new Editor({ element: noteHost, extensions: noteExtensions(), content: { type: "doc", content: [{ type: "paragraph", content: [{ type: "text", text: "Map" }] }, { type: "sketch", attrs: sketch }] } });
  await frame();
  const svg = noteHost.querySelector(".nw-sketch svg");
  const figure = noteHost.querySelector(".nw-sketch").getBoundingClientRect();
  record("sketches", "the note shows every stroke, the drawing's shape kept", svg?.querySelectorAll("path").length === sketch.strokes.length && Math.abs(figure.height / figure.width - aspect) < 0.02 && noteHost.querySelector(".nw-sketch-empty").hidden && getComputedStyle(noteHost.querySelector(".nw-sketch-empty")).display === "none", { paths: svg?.querySelectorAll("path").length, strokes: sketch.strokes.length, ratio: figure.height / figure.width, aspect });
  record("sketches", "the Markdown mirror says [Sketch]", editor.markdown.serialize(editor.getJSON()).includes("[Sketch]"));
  editor.destroy();
  noteHost.remove();
}

async function locking() {
  if (!lockingAvailable()) {
    record("locking", "WebCrypto is available", false, { reason: "no crypto.subtle (not a secure context?)" });
    return;
  }
  const report = new URLSearchParams(location.search).get("report");
  if (!report) {
    record("locking", "the server's vectors", null, { reason: "run through bench/notes-browsers.mjs, which serves them" });
    return;
  }
  const vectors = await (await fetch(new URL("vectors", report))).json();
  const k = vectors.kek;
  const kek = await deriveKekBytes(k.password, fromBase64(k.salt), k.iterations);
  record("locking", "the key-encryption key is the server's", toBase64(kek) === k.kek);
  const wrap = { kdf: "pbkdf2-sha256", salt: k.salt, iterations: k.iterations, wrapped_dek: vectors.wrap.wrapped_dek, key_version: 1 };
  const key = await openWrap(k.password, wrap);
  const c = vectors.content;
  const ct = await encryptWith(key, fromBase64(c.iv), c.aad, new TextEncoder().encode(c.plaintext));
  record("locking", "the unwrapped key seals exactly what the server sealed", toBase64(ct) === c.ct);
  const opened = await openContent(key, c.note_id, c.key_version, { v: 1, iv: c.iv, ct: c.ct });
  record("locking", "the server's ciphertext opens here", JSON.stringify(opened) === c.plaintext);
  const file = await openFile(key, vectors.file.aid, fromBase64(vectors.file.blob));
  record("locking", "the server's encrypted file opens here", toBase64(file) === vectors.file.bytes);
  let wrong = false;
  try {
    await openWrap("not the password", wrap);
  } catch {
    wrong = true;
  }
  record("locking", "a wrong password opens nothing", wrong);
  const sealed = await sealContent(key, "note-a", 1, { doc: { type: "doc" } });
  let moved = false;
  try {
    await openContent(key, "note-b", 1, sealed);
  } catch {
    moved = true;
  }
  record("locking", "ciphertext opens only as its own note", moved);
  record("locking", "the key can't be exported", key.extractable === false);
  // The real cost of a derivation here (the server does the same per unlock).
  const begun = performance.now();
  await deriveKekBytes("a profile password", crypto.getRandomValues(new Uint8Array(16)), 600_000);
  const ms = Math.round(performance.now() - begun);
  record("locking", "a 600,000-round derivation finishes", ms < 20_000, { ms });
}

async function main() {
  try {
    fidelity();
  } catch (error) {
    record("fidelity", "crashed", false, { error: String(error?.stack || error) });
  }
  try {
    await journal();
  } catch (error) {
    record("journal", "crashed", false, { error: String(error?.stack || error) });
  }
  try {
    chords();
  } catch (error) {
    record("chords", "crashed", false, { error: String(error?.stack || error) });
  }
  try {
    await attachments();
  } catch (error) {
    record("attachments", "crashed", false, { error: String(error?.stack || error) });
  }
  try {
    await sketches();
  } catch (error) {
    record("sketches", "crashed", false, { error: String(error?.stack || error) });
  }
  try {
    await locking();
  } catch (error) {
    record("locking", "crashed", false, { error: String(error?.stack || error) });
  }
  const summary = {
    userAgent: navigator.userAgent,
    passed: results.filter((r) => r.pass === true).length,
    failed: results.filter((r) => r.pass === false).length,
    untestable: results.filter((r) => r.pass === null).length,
    results,
  };
  window.__suite = summary;
  document.getElementById("out").textContent = JSON.stringify(summary, null, 2);
  const report = new URLSearchParams(location.search).get("report");
  if (report) {
    await fetch(report, { method: "POST", body: JSON.stringify(summary), mode: "cors" }).catch(() => {});
  }
}

main();
