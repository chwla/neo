/**
 * The Notes controller against a scripted server whose replies can be held.
 *
 * The server handles each request the moment it arrives, in arrival order, and
 * a held reply is only delivered later, which is how a slow network reorders
 * replies. What must hold whatever order replies arrive in:
 *   - only the newest open, and the newest view's list, ever reach the screen;
 *   - a reply that left the server before a newer change (a save reply racing a
 *     pin, a list fetched before a delete) never puts back what it replaced;
 *   - pinning, moving or archiving while typing keeps both the typing and the
 *     change, and a delete waits for the note's pending save.
 */
import assert from "node:assert/strict";
import { afterEach, describe, test } from "node:test";

const storage = new Map();
globalThis.localStorage = {
  getItem: (key) => (storage.has(key) ? storage.get(key) : null),
  setItem: (key, value) => storage.set(key, String(value)),
  removeItem: (key) => storage.delete(key),
};

const { LAST_VIEW_KEY, createNotesController, matchesView, mergeServerNote, viewQuery } = await import(
  "../src/notes/notesController.js"
);

const settle = async () => {
  for (let i = 0; i < 30; i += 1) await new Promise((resolve) => setImmediate(resolve));
};

function docFor(title, text) {
  return {
    type: "doc",
    content: [
      { type: "heading", attrs: { level: 1 }, content: [{ type: "text", text: title }] },
      { type: "paragraph", content: [{ type: "text", text }] },
    ],
  };
}

function seedNote(id, fields = {}) {
  const title = fields.title ?? `Note ${id}`;
  return {
    id,
    title,
    body: "Body",
    doc: docFor(title, "Body"),
    tags: [],
    summary: null,
    content_format: "rich",
    revision: 1,
    meta_revision: 1,
    pinned: false,
    archived: false,
    deleted: false,
    deleted_at: null,
    folder_id: null,
    created_at: "2026-10-01T10:00:00Z",
    updated_at: "2026-10-01T10:00:00Z",
    ...fields,
  };
}

function abortError() {
  const error = new Error("aborted");
  error.name = "AbortError";
  return error;
}

function fakeServer(seed) {
  const notes = new Map(seed.map((note) => [note.id, structuredClone(note)]));
  const log = [];
  const holds = [];
  let clock = Date.parse("2026-10-15T09:00:00Z");
  const stamp = () => new Date((clock += 1000)).toISOString();
  const listed = (note) => {
    const { doc, body, ...rest } = note;
    return rest;
  };

  function handle({ method, path, query, body }) {
    const json = (status, payload) => ({ status, payload });
    if (path === "/notes/settings") return json(200, method === "PATCH" ? body : {});
    if (path === "/notes/sidebar") return json(200, { folders: [], counts: { all: notes.size, default: 0, archived: 0, deleted: 0 }, tags: [] });
    if (path === "/notes" && method === "GET") {
      const view = query.get("view");
      const folder = query.get("folder_id");
      const rows = [...notes.values()].filter((note) => {
        if (view === "deleted") return note.deleted;
        if (note.deleted) return false;
        if (view === "archived") return note.archived;
        if (note.archived) return false;
        if (view === "folder") return (note.folder_id ?? "default") === folder;
        return true;
      });
      return json(200, { notes: rows.map(listed), total: rows.length });
    }
    const match = path.match(/^\/notes\/([^/]+)(?:\/(\w+))?$/);
    const note = match && notes.get(match[1]);
    if (!note) return json(404, { detail: "Note not found." });
    const action = match[2];
    if (!action && method === "GET") return json(200, { note: structuredClone(note) });
    if (action === "content" && method === "PUT") {
      if (note.deleted) return json(409, { detail: { reason: "note_deleted" } });
      if (body.base_revision !== note.revision) return json(409, { detail: { reason: "stale" } });
      Object.assign(note, { title: body.title, body: body.body, doc: body.doc, tags: body.tags, summary: body.summary });
      note.revision += 1;
      note.updated_at = stamp();
      return json(200, { note: structuredClone(note) });
    }
    if (method === "POST" && ["pin", "archive", "move"].includes(action)) {
      if (action === "pin") note.pinned = body.pinned;
      if (action === "archive") note.archived = body.archived;
      if (action === "move") note.folder_id = body.folder_id;
      note.meta_revision += 1;
      return json(200, { note: structuredClone(note) });
    }
    if (method === "DELETE" && !action) {
      note.deleted = true;
      note.deleted_at = stamp();
      note.meta_revision += 1;
      return json(200, { deleted: true, deleted_at: note.deleted_at, meta_revision: note.meta_revision });
    }
    if (action === "restore") {
      Object.assign(note, { deleted: false, deleted_at: null });
      note.meta_revision += 1;
      return json(200, { note: structuredClone(note) });
    }
    if (action === "conflicts") return json(200, []);
    return json(404, { detail: `no route ${method} ${path}` });
  }

  async function fetch(url, options = {}) {
    const parsed = new URL(url, "http://neo.test");
    const request = {
      method: options.method ?? "GET",
      path: parsed.pathname.replace(/^\/api/, ""),
      query: parsed.searchParams,
      body: options.body ? JSON.parse(options.body) : null,
    };
    log.push({ method: request.method, path: request.path, search: parsed.search });
    if (options.signal?.aborted) throw abortError();
    // Handled now, in arrival order; a hold only delays the delivery.
    const reply = handle(request);
    const hold = holds.find((entry) => !entry.used && entry.match(request));
    if (hold) {
      hold.used = true;
      await new Promise((resolve, reject) => {
        hold.deliver = resolve;
        options.signal?.addEventListener("abort", () => reject(abortError()), { once: true });
      });
    }
    return new Response(JSON.stringify(reply.payload), {
      status: reply.status,
      headers: { "content-type": "application/json" },
    });
  }

  return {
    notes,
    log,
    fetch,
    /** Hold the reply to the next request `match` accepts; returns a function that delivers it. */
    hold(match) {
      const entry = { match, used: false, deliver: null };
      holds.push(entry);
      return async () => {
        while (!entry.deliver) await settle();
        entry.deliver();
        await settle();
      };
    },
  };
}

let profileSeq = 0;
const controllers = [];

async function setup(seed) {
  const server = fakeServer(seed);
  globalThis.fetch = server.fetch;
  const controller = createNotesController(`test-${process.pid}-${(profileSeq += 1)}`);
  controllers.push(controller);
  await controller.ensureReady();
  return { server, controller, state: () => controller.getState() };
}

/** Stand in for the editor: its document is whatever `text` says now. */
function attachFakeEditor(controller, title) {
  const editor = { title, text: "Body" };
  const key = controller.getState().session.key;
  controller.attachEditor(key, () => ({
    title: editor.title,
    body: editor.text,
    tags: [],
    doc: docFor(editor.title, editor.text),
  }));
  return {
    type(text) {
      editor.text = text;
      controller.edit(key);
    },
  };
}

afterEach(async () => {
  while (controllers.length) await controllers.pop().dispose();
  storage.clear();
});

describe("views", () => {
  test("which notes belong to a view", () => {
    const plain = { deleted: false, archived: false, folder_id: null, tags: ["work"] };
    assert.ok(matchesView(plain, { kind: "all" }));
    assert.ok(matchesView(plain, { kind: "folder", folderId: "default" }));
    assert.ok(!matchesView(plain, { kind: "folder", folderId: "f1" }));
    assert.ok(matchesView({ ...plain, folder_id: "f1" }, { kind: "folder", folderId: "f1" }));
    assert.ok(!matchesView({ ...plain, archived: true }, { kind: "all" }));
    assert.ok(matchesView({ ...plain, archived: true }, { kind: "archived" }));
    assert.ok(matchesView({ ...plain, deleted: true, deleted_at: "x" }, { kind: "deleted" }));
    // Deleted by the old UI (no deleted_at): hidden everywhere, as on the server.
    assert.ok(!matchesView({ ...plain, deleted: true, deleted_at: null }, { kind: "deleted" }));
    assert.ok(matchesView(plain, { kind: "tags", tags: ["work", "home"], tagsMode: "any" }));
    assert.ok(!matchesView(plain, { kind: "tags", tags: ["work", "home"], tagsMode: "all" }));
    assert.ok(matchesView({ ...plain, archived: true }, { kind: "folder", folderId: "x", q: "milk" }));
    assert.ok(!matchesView({ ...plain, deleted: true }, { kind: "all", q: "milk" }));
  });

  test("each view asks the server for the same notes", () => {
    const settings = { sort_by: "edited", sort_order: "desc" };
    assert.deepEqual(viewQuery({ kind: "all" }, settings), { sort: "edited", order: "desc" });
    assert.deepEqual(viewQuery({ kind: "folder", folderId: "f1" }, settings), { view: "folder", folderId: "f1", sort: "edited", order: "desc" });
    assert.deepEqual(viewQuery({ kind: "deleted" }, settings), { view: "deleted", pinnedFirst: false, sort: "edited", order: "desc" });
    assert.deepEqual(viewQuery({ kind: "all", q: "milk" }, settings), { q: "milk", includeArchived: true, sort: "edited", order: "desc" });
    assert.deepEqual(viewQuery({ kind: "tags", tags: ["a"], tagsMode: "all" }, settings), {
      view: "tag",
      tags: ["a"],
      tagsMode: "all",
      sort: "edited",
      order: "desc",
    });
  });

  test("the remembered view has the key App clears on a profile switch", () => {
    assert.equal(LAST_VIEW_KEY, "neo-notes-last-view");
  });
});

describe("merging server facts", () => {
  const shown = { id: "a", title: "Typed", preview: "p", revision: 3, meta_revision: 5, pinned: true, folder_id: "f1" };

  test("metadata older than what is shown is ignored, content is judged on its own", () => {
    const merged = mergeServerNote(shown, { id: "a", title: "Saved", revision: 4, meta_revision: 4, pinned: false, folder_id: null });
    assert.equal(merged.pinned, true);
    assert.equal(merged.folder_id, "f1");
    assert.equal(merged.title, "Saved");
    assert.equal(merged.revision, 4);
  });

  test("content older than what is shown is ignored, metadata is judged on its own", () => {
    const merged = mergeServerNote(shown, { id: "a", title: "Old", revision: 2, meta_revision: 6, pinned: false });
    assert.equal(merged.title, "Typed");
    assert.equal(merged.pinned, false);
  });

  test("a field with a write in flight keeps the value asked for", () => {
    const merged = mergeServerNote(shown, { id: "a", revision: 3, meta_revision: 9, pinned: false, folder_id: null }, { pending: { pinned: true } });
    assert.equal(merged.pinned, true);
    assert.equal(merged.folder_id, null);
    assert.equal(merged.meta_revision, 9);
  });

  test("while the editor is ahead, its title and preview stay", () => {
    const merged = mergeServerNote(shown, { id: "a", title: "Saved", preview: "q", revision: 4, meta_revision: 5 }, { live: true });
    assert.equal(merged.title, "Typed");
    assert.equal(merged.preview, "p");
    assert.equal(merged.revision, 4);
  });

  test("documents are never kept in list rows", () => {
    const merged = mergeServerNote(undefined, { id: "a", doc: { type: "doc" }, body: "x", revision: 1, meta_revision: 1 });
    assert.ok(!("doc" in merged) && !("body" in merged));
  });
});

describe("opening and listing", () => {
  test("a newer open wins over a slower earlier one", async () => {
    const { server, controller, state } = await setup([seedNote("a"), seedNote("b")]);
    const deliverA = server.hold((request) => request.path === "/notes/a" && request.method === "GET");
    const first = controller.openNote("a");
    await settle();
    await controller.openNote("b");
    await deliverA();
    await first;
    assert.equal(state().session.id, "b");
    assert.equal(state().selection.id, "b");
    assert.equal(state().session.note.title, "Note b");
  });

  test("a list for an earlier view never replaces the newer view's", async () => {
    const { server, controller, state } = await setup([seedNote("a"), seedNote("b", { folder_id: "f1" })]);
    const deliverFolder = server.hold((request) => request.path === "/notes" && request.query.get("folder_id") === "f1");
    controller.setView({ kind: "folder", folderId: "f1" });
    await settle();
    controller.setView({ kind: "all" });
    await settle();
    await deliverFolder().catch(() => {});
    await settle();
    assert.equal(state().view.kind, "all");
    assert.deepEqual([...state().list.ids].sort(), ["a", "b"]);
  });

  test("changing to a view the open note is not in closes it", async () => {
    const { controller, state } = await setup([seedNote("a")]);
    await controller.openNote("a");
    controller.setView({ kind: "folder", folderId: "f1" });
    assert.equal(state().session, null);
    controller.setView({ kind: "all" });
    await controller.openNote("a");
    controller.setView({ kind: "folder", folderId: "default" });
    assert.equal(state().session.id, "a");
  });

  test("a new note starts in the folder being viewed", async () => {
    const { controller, state } = await setup([]);
    controller.setView({ kind: "folder", folderId: "f1" });
    const key = controller.newNote();
    assert.equal(state().drafts[key].folder_id, "f1");
    assert.equal(state().session.note.folder_id, "f1");
  });

  test("an offline draft reopens with its unsaved text after switching notes", async () => {
    const { controller, state } = await setup([seedNote("a")]);
    const key = controller.newNote({ folderId: "f1" });
    const editor = attachFakeEditor(controller, "Offline draft");
    editor.type("Keep this text");
    const fetch = globalThis.fetch;
    globalThis.fetch = (url, options = {}) => {
      if (new URL(url, "http://neo.test").pathname === "/api/notes" && options.method === "POST") {
        return Promise.reject(new TypeError("offline"));
      }
      return fetch(url, options);
    };
    try {
      await controller.openNote("a");
      await settle();
      assert.ok(state().drafts[key]);
      assert.equal(state().session.id, "a");

      controller.openDraft(key);
      assert.equal(state().selection.key, key);
      assert.equal(state().session.key, key);
      assert.equal(state().session.note.folder_id, "f1");
      assert.equal(state().session.doc.content[1].content[0].text, "Keep this text");
    } finally {
      globalThis.fetch = fetch;
    }
  });
});

describe("metadata while typing", () => {
  test("a save reply that left the server before a pin does not unpin", async () => {
    const { server, controller, state } = await setup([seedNote("a")]);
    await controller.refreshList();
    await controller.openNote("a");
    const editor = attachFakeEditor(controller, "Note a");
    editor.type("Typed before the pin");
    const deliverSave = server.hold((request) => request.path === "/notes/a/content");
    const saving = controller.flushCurrent();
    await settle();
    controller.pin("a", true);
    await settle();
    assert.equal(state().items.a.pinned, true);
    await deliverSave();
    await saving;
    await settle();
    assert.equal(server.notes.get("a").pinned, true);
    assert.equal(server.notes.get("a").body, "Typed before the pin");
    assert.equal(state().items.a.pinned, true, "the older save reply must not unpin");
    assert.equal(state().session.note.pinned, true);
  });

  test("a list fetched before a pin does not undo it", async () => {
    const { server, controller, state } = await setup([seedNote("a")]);
    await controller.refreshList();
    const deliverList = server.hold((request) => request.path === "/notes" && request.method === "GET");
    const listing = controller.refreshList();
    await settle();
    controller.pin("a", true);
    await settle();
    await deliverList();
    await listing;
    assert.equal(state().items.a.pinned, true);
  });

  test("a list fetched before a delete does not bring the note back", async () => {
    const { server, controller, state } = await setup([seedNote("a"), seedNote("b")]);
    await controller.refreshList();
    const deliverList = server.hold((request) => request.path === "/notes" && request.method === "GET");
    const listing = controller.refreshList();
    await settle();
    await controller.trash("a");
    await deliverList();
    await listing;
    assert.equal(state().items.a.deleted, true);
    assert.ok(!matchesView(state().items.a, state().view));
    assert.ok(matchesView(state().items.b, state().view));
  });

  test("moving while typing keeps both the move and the typing", async () => {
    const { server, controller, state } = await setup([seedNote("a")]);
    await controller.refreshList();
    await controller.openNote("a");
    const editor = attachFakeEditor(controller, "Note a");
    editor.type("First words");
    const deliverMove = server.hold((request) => request.path === "/notes/a/move");
    controller.move("a", "f1");
    await settle();
    editor.type("First words and more");
    await controller.flushCurrent();
    await deliverMove();
    await settle();
    const stored = server.notes.get("a");
    assert.equal(stored.folder_id, "f1");
    assert.equal(stored.body, "First words and more");
    assert.equal(state().items.a.folder_id, "f1");
  });

  test("archiving and unarchiving quickly ends as the last choice", async () => {
    const { server, controller, state } = await setup([seedNote("a")]);
    await controller.refreshList();
    const deliverFirst = server.hold((request) => request.path === "/notes/a/archive");
    controller.archive("a", true);
    await settle();
    controller.archive("a", false);
    await deliverFirst();
    await settle();
    assert.equal(server.notes.get("a").archived, false);
    assert.equal(state().items.a.archived, false);
  });

  test("a delete waits for the note's pending save", async () => {
    const { server, controller } = await setup([seedNote("a")]);
    await controller.refreshList();
    await controller.openNote("a");
    attachFakeEditor(controller, "Note a").type("Last words");
    await controller.trash("a");
    const order = server.log.filter((entry) => entry.path === "/notes/a/content" || entry.method === "DELETE").map((entry) => entry.method);
    assert.deepEqual(order, ["PUT", "DELETE"]);
    assert.equal(server.notes.get("a").body, "Last words");
    assert.equal(server.notes.get("a").deleted, true);
  });

  test("pinning a note opened from elsewhere works before the list has it", async () => {
    const { server, controller, state } = await setup([seedNote("z")]);
    await controller.openNote("z");
    assert.equal(state().items.z, undefined);
    controller.pin("z", true);
    await settle();
    assert.equal(server.notes.get("z").pinned, true);
    assert.equal(state().items.z.pinned, true);
  });
});
