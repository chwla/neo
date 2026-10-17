import assert from "node:assert/strict";
import { test } from "node:test";

import { insertAttachments } from "../src/notes/extensions/attachments.js";

function editorHarness() {
  const inserted = [];
  const transaction = { setMeta() { return this; } };
  const editor = {
    isDestroyed: false,
    isEditable: true,
    state: { selection: { to: 1 }, doc: { content: { size: 10 } }, tr: transaction },
    view: { dispatch() {} },
    chain() {
      return {
        insertContentAt(pos, node) {
          inserted.push({ pos, node });
          return this;
        },
        run() {},
      };
    },
  };
  return { editor, inserted };
}

const file = { name: "plan.txt", type: "text/plain", size: 4 };
const attachment = { kind: "file", id: "a", name: file.name, size: file.size, mime_type: file.type };

test("an upload finishing after a note switch cannot insert into the next note", async () => {
  const { editor, inserted } = editorHarness();
  const errors = [];
  let token = "note-a";
  let finishUpload;
  let uploadStarted;
  const started = new Promise((resolve) => { uploadStarted = resolve; });
  const pending = insertAttachments(editor, [file], {
    currentNoteToken: () => token,
    upload: () => {
      uploadStarted();
      return new Promise((resolve) => { finishUpload = resolve; });
    },
    onError: (error) => errors.push(error),
  });
  await started;
  token = "note-b";
  finishUpload({ attachment });
  await pending;
  assert.deepEqual(inserted, []);
  assert.match(errors[0], /switched notes/);
});

test("a switch during picture preparation prevents the upload entirely", async () => {
  const { editor, inserted } = editorHarness();
  let token = "note-a";
  let uploads = 0;
  const pending = insertAttachments(editor, [file], {
    currentNoteToken: () => token,
    upload: async () => { uploads += 1; return { attachment }; },
  });
  token = "note-b";
  await pending;
  assert.equal(uploads, 0);
  assert.deepEqual(inserted, []);
});

test("an upload staying in its note inserts at the saved position", async () => {
  const { editor, inserted } = editorHarness();
  await insertAttachments(editor, [file], {
    currentNoteToken: () => "note-a",
    upload: async () => ({ attachment }),
  });
  assert.equal(inserted.length, 1);
  assert.equal(inserted[0].pos, 1);
  assert.equal(inserted[0].node.attrs.fileId, attachment.id);
});
