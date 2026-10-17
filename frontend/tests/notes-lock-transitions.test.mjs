/**
 * Lock and Remove Lock from the browser's side, against a stand-in server that
 * applies transitions as the real one does (the real one is tested in
 * tests/test_notes_transitions.py).
 *
 * What must hold: a lock encrypts the note, every version and conflict and
 * every attachment, and opens back to exactly what it was; a failure at any
 * step before the commit aborts it and is reported, and a failure at or after
 * it is never reported as success; Remove Lock returns the plaintext, older
 * Markdown included, exactly.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { openContent, openFile, sha256Hex } from "../src/notes/crypto.js";
import { LockError, lockNote, removeLock } from "../src/notes/lockTransitions.js";

const F1 = "11111111-1111-4111-8111-111111111111";
const F2 = "22222222-2222-4222-8222-222222222222";
const enc = new TextEncoder();

async function aesKey() {
  return crypto.subtle.generateKey({ name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
}

function fakeServer() {
  let nextId = 1;
  const id = () => `00000000-0000-4000-8000-${String(nextId++).padStart(12, "0")}`;
  const files = new Map([
    [F1, enc.encode("png bytes of the map")],
    [F2, enc.encode("tickets pdf")],
  ]);
  const note = {
    id: "note-1",
    title: "Trip",
    content_format: "rich",
    revision: 5,
    summary: null,
    doc: {
      type: "doc",
      content: [
        { type: "heading", attrs: { level: 1 }, content: [{ type: "text", text: "Trip" }] },
        { type: "paragraph", content: [{ type: "text", text: "Gate code 4512" }] },
        { type: "image", attrs: { src: `/api/files/${F1}/download`, fileId: F1 } },
        { type: "fileAttachment", attrs: { fileId: F2, name: "tickets.pdf", size: 11, mime: "application/pdf" } },
      ],
    },
  };
  const versions = [{ id: "v1", title: "Trip", content_format: "markdown", body: `Packing list ![map](/api/files/${F1}/download)`, tags: ["travel"], summary: null }];
  const conflicts = [{ id: "c1", title: "Trip", content_format: "rich", doc: { type: "doc", content: [{ type: "paragraph", content: [{ type: "text", text: "stale" }] }] }, summary: null }];
  const transitions = new Map();
  const fail = {};
  const calls = [];
  const maybeFail = async (name, ...args) => {
    calls.push(name);
    const f = fail[name];
    if (typeof f === "function") {
      const out = await f(...args);
      if (out !== undefined) return out;
    } else if (f) throw f;
    return undefined;
  };
  const api = {
    async note() {
      await maybeFail("note");
      return { note: structuredClone(note) };
    },
    async noteVersions() {
      return versions.map((v) => ({ id: v.id }));
    },
    async noteVersion(_n, vid) {
      return structuredClone(versions.find((v) => v.id === vid));
    },
    async noteConflicts(_n, { includeResolved } = {}) {
      assert.equal(includeResolved, true, "resolved conflicts hold content too");
      return structuredClone(conflicts);
    },
    async fileBytes(fileId) {
      const injected = await maybeFail("fileBytes", fileId);
      if (injected) return injected;
      if (!files.has(fileId)) throw Object.assign(new Error("gone"), { status: 404 });
      return files.get(fileId).slice();
    },
    async beginNoteTransition(_n, { kind, base_revision }) {
      await maybeFail("begin");
      if (base_revision !== note.revision) throw Object.assign(new Error("stale"), { status: 409 });
      const t = { id: `t${transitions.size + 1}`, kind, base: base_revision, state: "begun", staged: new Map() };
      transitions.set(t.id, t);
      return { id: t.id };
    },
    async stageNoteTransitionFile(_n, tid, blob) {
      const injected = await maybeFail("stage");
      if (injected) return injected;
      const bytes = new Uint8Array(await blob.arrayBuffer());
      const fileId = id();
      files.set(fileId, bytes);
      transitions.get(tid).staged.set(fileId, bytes);
      return { file_id: fileId, sha256: await sha256Hex(bytes), size: bytes.length };
    },
    async commitNoteTransition(_n, tid, payload) {
      await maybeFail("commit");
      const t = transitions.get(tid);
      if (t.state === "committed") return t.result;
      if (note.revision !== t.base) {
        t.state = "aborted";
        throw Object.assign(new Error("stale"), { status: 409 });
      }
      const apply = (target, form) => {
        if (t.kind === "lock") Object.assign(target, { content_format: "encrypted", cipher: form.cipher, lock_key_version: form.lock_key_version, doc: null, body: "" });
        else Object.assign(target, { content_format: form.format, cipher: null, lock_key_version: null, doc: form.doc ?? null, body: form.body, tags: form.tags });
      };
      apply(note, payload.content);
      for (const v of versions) apply(v, payload.versions[v.id]);
      for (const c of conflicts) apply(c, payload.conflicts[c.id]);
      note.revision += 1;
      t.state = "committed";
      t.result = { revision: note.revision, content_format: note.content_format };
      return t.result;
    },
    async abortNoteTransition(_n, tid) {
      calls.push("abort");
      transitions.get(tid).state = "aborted";
    },
  };
  return { api, note, versions, conflicts, files, transitions, fail, calls };
}

async function locked() {
  const server = fakeServer();
  const key = await aesKey();
  const note = await lockNote({ api: server.api, noteId: "note-1", baseRevision: 5, key, keyVersion: 2 });
  return { server, key, note };
}

describe("lock", () => {
  test("everything is encrypted, and opens back to exactly what it was", async () => {
    const { server, key, note } = await locked();
    assert.equal(note.content_format, "encrypted");
    assert.equal(note.revision, 6);
    const opened = await openContent(key, "note-1", 2, note.cipher);
    assert.equal(opened.format, "rich");
    assert.equal(opened.doc.content[1].content[0].text, "Gate code 4512");
    const image = opened.doc.content[2].attrs;
    const chip = opened.doc.content[3].attrs;
    assert.notEqual(image.fileId, F1);
    assert.equal(image.src, `/api/files/${image.fileId}/download`);
    assert.deepEqual(await openFile(key, image.aid, server.files.get(image.fileId)), enc.encode("png bytes of the map"));
    assert.deepEqual(await openFile(key, chip.aid, server.files.get(chip.fileId)), enc.encode("tickets pdf"));
    assert.equal(chip.name, "tickets.pdf", "the name lives inside the encrypted note");

    const version = await openContent(key, "note-1", 2, server.versions[0].cipher);
    assert.equal(version.format, "markdown");
    assert.ok(version.body.startsWith("Packing list ![map](/api/files/"));
    assert.equal(version.files[image.fileId], image.aid, "an older Markdown note keeps its attachment's key");
    assert.equal((await openContent(key, "note-1", 2, server.conflicts[0].cipher)).doc.content[0].content[0].text, "stale");
  });

  for (const [step, failure] of [
    ["begin", Object.assign(new Error("stale"), { status: 409 })],
    ["fileBytes", new Error("network")],
    ["stage", new Error("disk full")],
    ["stage", async () => ({ file_id: "x", sha256: "not what was sent", size: 1 })],
    ["fileBytes", async (fileId) => (fileId.startsWith("00000000") ? new Uint8Array([1, 2, 3]) : undefined)],
    ["commit", Object.assign(new Error("stale"), { status: 409 })],
    ["commit", new Error("offline")],
  ]) {
    test(`a failure at ${step} (${failure.message ?? "a wrong answer"}) is reported, never as success`, async () => {
      const server = fakeServer();
      server.fail[step] = failure;
      const key = await aesKey();
      await assert.rejects(lockNote({ api: server.api, noteId: "note-1", baseRevision: 5, key, keyVersion: 2 }), LockError);
      const committed = [...server.transitions.values()].some((t) => t.state === "committed");
      if (step !== "commit" && step !== "begin") assert.ok(server.calls.includes("abort"), "aborted before commit");
      if (!committed) {
        assert.equal(server.note.content_format, "rich", "the note is as it was");
        assert.equal(server.note.doc.content[1].content[0].text, "Gate code 4512");
      }
    });
  }

  test("a lock the server holds differently from what was sent is not reported as done", async () => {
    const server = fakeServer();
    let reads = 0;
    server.fail.note = async () => {
      reads += 1;
      // The read back after commit sees something else.
      if (reads === 2) server.note.cipher = { v: 1, iv: "x", ct: "something else" };
    };
    await assert.rejects(lockNote({ api: server.api, noteId: "note-1", baseRevision: 5, key: await aesKey(), keyVersion: 2 }), /couldn't be confirmed/);
  });

  test("a note that changed after it was read is not locked", async () => {
    const server = fakeServer();
    server.fail.note = async () => {
      server.note.revision = 6;
    };
    await assert.rejects(lockNote({ api: server.api, noteId: "note-1", baseRevision: 5, key: await aesKey(), keyVersion: 2 }), /changed/);
    assert.ok(server.calls.includes("abort"));
  });
});

describe("Remove Lock", () => {
  test("the plaintext comes back exactly, attachments and older Markdown included", async () => {
    const { server, key } = await locked();
    const note = await removeLock({
      api: server.api,
      noteId: "note-1",
      baseRevision: 6,
      keyFor: (version) => (version === 2 ? key : null),
      project: () => ({ title: "Trip", body: "Trip\n\nGate code 4512", tags: [] }),
    });
    assert.equal(note.content_format, "rich");
    assert.equal(note.doc.content[1].content[0].text, "Gate code 4512");
    const image = note.doc.content[2].attrs;
    assert.equal(image.aid, null);
    assert.deepEqual(server.files.get(image.fileId), enc.encode("png bytes of the map"));
    assert.equal(server.versions[0].content_format, "markdown");
    assert.match(server.versions[0].body, /^Packing list !\[map\]\(\/api\/files\/[0-9a-f-]{36}\/download\)$/);
    assert.deepEqual(server.versions[0].tags, ["travel"]);
  });

  test("without the key for a part of the note, nothing changes", async () => {
    const { server } = await locked();
    await assert.rejects(
      removeLock({ api: server.api, noteId: "note-1", baseRevision: 6, keyFor: () => null, project: () => ({}) }),
      (error) => error instanceof LockError && error.code === "key",
    );
    assert.equal(server.note.content_format, "encrypted");
  });
});
