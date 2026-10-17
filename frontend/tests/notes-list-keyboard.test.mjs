import assert from "node:assert/strict";
import { test } from "node:test";

import { activateHighlightedNote } from "../src/notes/NoteList.jsx";

function enter() {
  return {
    key: "Enter",
    prevented: false,
    preventDefault() { this.prevented = true; },
  };
}

test("Enter opens a different saved note, then focuses it after a cold fetch", async () => {
  const item = { id: "other" };
  const event = enter();
  const frames = [];
  const opened = [];
  const focused = [];
  let finishOpen;
  const opening = new Promise((resolve) => { finishOpen = resolve; });

  const handled = activateHighlightedNote(
    event,
    item,
    (row) => { opened.push(row.id); return opening; },
    (row) => { focused.push(row.id); return focused.length > 1; },
    (callback) => frames.push(callback),
  );

  assert.equal(handled, true);
  assert.equal(event.prevented, true);
  assert.deepEqual(opened, ["other"]);
  assert.deepEqual(focused, []);
  frames.shift()();
  assert.deepEqual(focused, ["other"]);
  finishOpen();
  await opening;
  await Promise.resolve();
  assert.equal(frames.length, 1);
  frames.shift()();
  assert.deepEqual(focused, ["other", "other"]);
});

test("Enter opens a pending draft and focuses it on the next frame", () => {
  const item = { key: "draft-1" };
  const event = enter();
  const frames = [];
  const opened = [];
  const focused = [];

  activateHighlightedNote(
    event,
    item,
    (row) => { opened.push(row.key); return true; },
    (row) => { focused.push(row.key); return true; },
    (callback) => frames.push(callback),
  );

  assert.equal(event.prevented, true);
  assert.deepEqual(opened, ["draft-1"]);
  frames.shift()();
  assert.deepEqual(focused, ["draft-1"]);
  assert.equal(frames.length, 0);
});
