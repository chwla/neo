/**
 * Locked notes in the Notes controller, against a stand-in server.
 *
 * What must hold: a locked note shows nothing until its key is open, then
 * exactly its content; every edit leaves this page as ciphertext only; Lock
 * Now (and auto-lock, and a profile switch) puts the note back behind its
 * lock with nothing decrypted left in the session, sealing unsaved edits first.
 */
import assert from "node:assert/strict";
import { afterEach, describe, test } from "node:test";

import { deriveKekBytes, openContent, sealContent, toBase64 } from "../src/notes/crypto.js";

globalThis.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
const { createNotesController } = await import("../src/notes/notesController.js");

const PASSWORD = "profile-password";
const SECRET = "gate code 4512";
const settle = async () => {
  for (let i = 0; i < 40; i += 1) await new Promise((resolve) => setImmediate(resolve));
};

/** A key version as the server stores it: a random data key, wrapped under the password. */
async function makeKey() {
  const dek = await crypto.subtle.generateKey({ name: "AES-GCM", length: 256 }, true, ["encrypt", "decrypt"]);
  /** The same data key, wrapped under a password (as the server does on setup and re-wrap). */
  const wrapFor = async (password) => {
    const salt = crypto.getRandomValues(new Uint8Array(16));
    const kek = await crypto.subtle.importKey("raw", await deriveKekBytes(password, salt, 1000), "AES-KW", false, ["wrapKey"]);
    const wrapped = new Uint8Array(await crypto.subtle.wrapKey("raw", dek, kek, "AES-KW"));
    return { key_version: 1, kdf: "pbkdf2-sha256", salt: toBase64(salt), iterations: 1000, wrapped_dek: toBase64(wrapped) };
  };
  return { dek, wrap: await wrapFor(PASSWORD), wrapFor };
}

function docOf(text) {
  return { type: "doc", content: [{ type: "heading", attrs: { level: 1 }, content: [{ type: "text", text: "Trip" }] }, { type: "paragraph", content: [{ type: "text", text }] }] };
}

async function fakeServer({ replacedPassword = false } = {}) {
  const key = await makeKey();
  const note = {
    id: "locked-1",
    title: "Trip",
    content_format: "encrypted",
    lock_key_version: 1,
    cipher: await sealContent(key.dek, "locked-1", 1, { format: "rich", doc: docOf(SECRET), summary: null }),
    body: "",
    doc: null,
    tags: [],
    summary: null,
    revision: 3,
    meta_revision: 1,
    pinned: false,
    archived: false,
    deleted: false,
    deleted_at: null,
    folder_id: null,
    created_at: "2026-10-01T10:00:00Z",
    updated_at: "2026-10-01T10:00:00Z",
  };
  const writes = [];
  const rewrapped = await key.wrapFor("replaced-password");
  const handle = ({ method, path, body }) => {
    const json = (status, payload) => ({ status, payload });
    if (path === "/notes/settings") return json(200, {});
    if (path === "/notes/keys" && method === "GET") return json(200, { set_up: true, versions: [1], guest: false });
    if (path === "/notes/keys/unlock") {
      const current = replacedPassword ? "replaced-password" : PASSWORD;
      if (body.password !== current) return json(401, { detail: "That password does not match this profile." });
      // After a password replaced without the old one, version 1 opens with nothing current.
      if (replacedPassword) return json(200, { wraps: [], unavailable: [1], current_version: null });
      return json(200, { wraps: [key.wrap], unavailable: [], current_version: 1 });
    }
    if (path === "/notes/keys/rewrap") {
      if (body.password !== "replaced-password") return json(401, { detail: "That password does not match this profile." });
      if (body.earlier_password !== PASSWORD) return json(401, { detail: { code: "earlier_password_wrong", message: "That password doesn't open these notes." } });
      return json(200, { wraps: [rewrapped], unavailable: [], current_version: 1, recovered: [1] });
    }
    if (path === "/notes/sidebar") return json(200, { folders: [], counts: { all: 1, default: 1, archived: 0, deleted: 0 }, tags: [] });
    if (path === "/notes" && method === "GET") {
      const { cipher, ...row } = note;
      return json(200, { notes: [row], total: 1 });
    }
    if (path === `/notes/${note.id}` && method === "GET") return json(200, { note: structuredClone(note) });
    if (path === `/notes/${note.id}/content` && method === "PUT") {
      writes.push(body);
      if (body.format !== "encrypted") return json(409, { detail: { code: "locked" } });
      if (body.base_revision !== note.revision) return json(409, { detail: { code: "conflict", reason: "stale" } });
      Object.assign(note, { title: body.title, cipher: body.cipher, revision: note.revision + 1 });
      return json(200, { note: structuredClone(note) });
    }
    return json(404, { detail: `no route ${method} ${path}` });
  };
  globalThis.fetch = async (url, options = {}) => {
    const parsed = new URL(url, "http://neo.test");
    const reply = handle({ method: options.method ?? "GET", path: parsed.pathname.replace(/^\/api/, ""), body: options.body ? JSON.parse(options.body) : null });
    return new Response(JSON.stringify(reply.payload), { status: reply.status, headers: { "content-type": "application/json" } });
  };
  return { note, writes, key };
}

const controllers = [];
let seq = 0;
afterEach(async () => {
  while (controllers.length) await controllers.pop().dispose();
});

async function setup(options) {
  const server = await fakeServer(options);
  const controller = createNotesController(`lock-test-${process.pid}-${(seq += 1)}`);
  controllers.push(controller);
  await controller.ensureReady();
  await settle();
  return { server, controller, state: () => controller.getState() };
}

describe("locked notes in the controller", () => {
  test("a locked note shows nothing until its key is open, then its content", async () => {
    const { controller, state } = await setup();
    await controller.openNote("locked-1");
    await settle();
    assert.equal(state().session.locked, true);
    assert.equal(state().session.doc, null);
    controller.openLocked();
    assert.equal(state().lockPrompt.mode, "unlock");
    await controller.submitLockPassword("wrong");
    assert.match(state().lockPrompt.error, /does not match/);
    assert.equal(state().session.doc, null);
    await controller.submitLockPassword(PASSWORD);
    await settle();
    assert.equal(state().lockPrompt, null);
    assert.equal(state().session.locked, false);
    assert.equal(state().session.doc.content[1].content[0].text, SECRET);
  });

  test("edits leave as ciphertext only; Lock Now seals what is unsaved and drops the plaintext", async () => {
    const { server, controller, state } = await setup();
    await controller.openNote("locked-1");
    controller.openLocked();
    await controller.submitLockPassword(PASSWORD);
    await settle();
    const key = state().session.key;
    let text = `${SECRET} and more`;
    controller.attachEditor(key, () => ({ doc: docOf(text) }));
    controller.edit(key);
    await controller.flushCurrent();
    await settle();
    const [write] = server.writes;
    assert.equal(write.format, "encrypted");
    assert.equal(JSON.stringify(write).includes(SECRET), false, "no plaintext sent");
    assert.equal((await openContent(server.key.dek, "locked-1", 1, write.cipher)).doc.content[1].content[0].text, `${SECRET} and more`);

    // Typed, not yet saved, then Lock Now.
    text = `${SECRET} last words`;
    controller.edit(key);
    await controller.lockNow();
    await settle();
    assert.equal(state().session.locked, true);
    assert.equal(state().session.doc, null, "nothing decrypted is left in the session");
    assert.equal(state().lock.unlocked, false);
    // What was typed still reached the server, sealed.
    await settle();
    const last = server.writes.at(-1);
    assert.equal(last.format, "encrypted");
    assert.equal((await openContent(server.key.dek, "locked-1", 1, last.cipher)).doc.content[1].content[0].text, `${SECRET} last words`);
    assert.equal(server.writes.some((w) => JSON.stringify(w).includes(SECRET)), false);

    // Opened again: the newest content, not what the note held when it was first opened.
    controller.openLocked();
    await controller.submitLockPassword(PASSWORD);
    await settle();
    assert.equal(state().session.doc.content[1].content[0].text, `${SECRET} last words`);
  });

  test("a note locked under an earlier password asks for it, and opens with it", async () => {
    const { controller, state } = await setup({ replacedPassword: true });
    await controller.openNote("locked-1");
    controller.openLocked();
    await controller.submitLockPassword("replaced-password");
    await settle();
    assert.equal(state().lockPrompt?.mode, "earlier", "the current password alone doesn't open it");
    assert.equal(state().session.doc, null);
    await controller.submitLockPassword("replaced-password", "a wrong guess");
    assert.match(state().lockPrompt.error, /doesn't open these notes/);
    await controller.submitLockPassword("replaced-password", PASSWORD);
    await settle();
    assert.equal(state().lockPrompt, null);
    assert.equal(state().session.doc.content[1].content[0].text, SECRET);
  });

  test("a profile switch (the controller disposed) locks", async () => {
    const { controller, state } = await setup();
    await controller.openNote("locked-1");
    controller.openLocked();
    await controller.submitLockPassword(PASSWORD);
    await settle();
    assert.equal(state().lock.unlocked, true);
    controllers.pop();
    await controller.dispose();
    assert.equal(state().lock.unlocked, false);
    assert.equal(state().session.doc, null);
  });
});

