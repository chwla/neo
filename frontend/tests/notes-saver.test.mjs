/**
 * The note saver and the recovery journal, against a scripted server and clock.
 *
 * What must hold, whatever order replies arrive in:
 *   - nothing is sent that is not already in the journal;
 *   - one write per note in flight; edits made meanwhile follow, newest only;
 *   - an acknowledgement removes exactly what it acknowledged, never newer content;
 *   - failures retry without reverting anything; conflicts and gone notes stop the
 *     queue but never stop typing from being kept;
 *   - a create retried after a lost reply does not duplicate the note.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { createNoteSaver, replayJournal } from "../src/notes/noteSaver.js";
import { createJournal, memoryJournalAdapter } from "../src/notes/recoveryJournal.js";

function fakeClock() {
  let time = 0;
  let nextId = 1;
  const pending = new Map();
  const settle = async () => {
    for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve));
  };
  return {
    now: () => time,
    setTimeout(fn, ms) {
      const id = nextId++;
      pending.set(id, { at: time + ms, fn });
      return id;
    },
    clearTimeout(id) {
      pending.delete(id);
    },
    async advance(ms) {
      const until = time + ms;
      for (;;) {
        await settle();
        const due = [...pending.entries()].filter(([, t]) => t.at <= until).sort((a, b) => a[1].at - b[1].at);
        if (!due.length) break;
        const [id, timer] = due[0];
        pending.delete(id);
        time = Math.max(time, timer.at);
        timer.fn();
      }
      time = until;
      await settle();
    },
  };
}

/** A server whose replies the test releases one by one. */
function scriptedServer() {
  const calls = [];
  let revision = 0;
  const notes = new Map();
  function reply(call) {
    return new Promise((resolve, reject) => {
      call.resolve = resolve;
      call.reject = reject;
    });
  }
  return {
    calls,
    transport: {
      create(payload) {
        const call = { kind: "create", payload };
        calls.push(call);
        return reply(call);
      },
      write(id, payload) {
        const call = { kind: "write", id, payload };
        calls.push(call);
        return reply(call);
      },
    },
    ok(call, extra = {}) {
      revision += 1;
      const id = call.id ?? `note-${calls.indexOf(call)}`;
      const note = { id, revision: call.kind === "create" ? 0 : (call.payload.base_revision ?? 0) + 1, title: call.payload.title };
      notes.set(id, note);
      call.resolve({ note, replayed: false, ...extra });
      return note;
    },
    fail(call, status, detail) {
      const error = new Error(`status ${status}`);
      if (status) {
        error.status = status;
        error.detail = detail;
      }
      call.reject(error);
    },
  };
}

function setup({ journal = true } = {}) {
  const clock = fakeClock();
  const server = scriptedServer();
  const adapter = memoryJournalAdapter();
  const tabJournal = journal ? createJournal(adapter, { tabId: "tab-1" }) : null;
  const events = [];
  const saver = createNoteSaver({
    transport: server.transport,
    journal: tabJournal,
    tabId: "tab-1",
    timers: clock,
    now: clock.now,
    onEvent: (event) => events.push(event),
  });
  let content = "";
  const take = () => ({ title: "T", body: content, doc: { type: "doc", text: content }, tags: [] });
  const type = (key, text) => {
    content = text;
    saver.edit(key, take);
  };
  return { clock, server, adapter, journal: tabJournal, saver, events, type };
}

describe("note saver", () => {
  test("the journal keeps only the document; the Markdown projection is made once, when sending", async () => {
    const clock = fakeClock();
    const server = scriptedServer();
    const adapter = memoryJournalAdapter();
    let projections = 0;
    const saver = createNoteSaver({
      transport: server.transport,
      journal: createJournal(adapter, { tabId: "tab-1" }),
      tabId: "tab-1",
      timers: clock,
      now: clock.now,
      project: (doc) => {
        projections += 1;
        return { title: `T ${doc.text}`, body: `body ${doc.text}`, tags: ["x"] };
      },
    });
    saver.track("n1", { serverId: "n1", revision: 1 });
    let text = "";
    const take = () => ({ doc: { type: "doc", text } });
    // Typing with pauses long enough for the journal to write several times.
    for (const next of ["a", "ab", "abc", "abcd"]) {
      text = next;
      saver.edit("n1", take);
      await clock.advance(300);
    }
    const [entry] = await adapter.list();
    assert.equal(entry.snapshot.body, undefined, "no projection is journalled");
    assert.equal(projections, 0, "journal writes never project");
    await clock.advance(600);
    assert.equal(server.calls.length, 1);
    assert.equal(projections, 1);
    assert.deepEqual(
      { title: server.calls[0].payload.title, body: server.calls[0].payload.body, tags: server.calls[0].payload.tags },
      { title: "T abcd", body: "body abcd", tags: ["x"] },
    );
  });

  test("a burst of typing is one write of the newest content, journaled first", async () => {
    const { clock, server, adapter, saver, type } = setup();
    saver.track("n1", { serverId: "n1", revision: 4 });
    for (const text of ["a", "ab", "abc", "abcd"]) {
      type("n1", text);
      await clock.advance(100);
    }
    assert.equal(server.calls.length, 0);
    await clock.advance(600);
    assert.equal(server.calls.length, 1);
    const [call] = server.calls;
    assert.equal(call.payload.body, "abcd");
    assert.equal(call.payload.base_revision, 4);
    const [entry] = await adapter.list();
    assert.equal(entry.snapshot.body, "abcd");
    assert.equal(call.payload.write_id, `tab-1:n1:${entry.seq}`);
  });

  test("one write in flight; edits during it follow with the newest content and new base", async () => {
    const { clock, server, saver, type } = setup();
    saver.track("n1", { serverId: "n1", revision: 0 });
    type("n1", "first");
    await clock.advance(700);
    type("n1", "second");
    await clock.advance(700);
    type("n1", "third");
    await clock.advance(700);
    assert.equal(server.calls.length, 1, "no second request while one is in flight");
    server.ok(server.calls[0]);
    await clock.advance(10);
    assert.equal(server.calls.length, 2);
    assert.equal(server.calls[1].payload.body, "third");
    assert.equal(server.calls[1].payload.base_revision, 1);
  });

  test("an acknowledgement never removes newer journal content", async () => {
    const { clock, server, adapter, saver, type } = setup();
    saver.track("n1", { serverId: "n1", revision: 0 });
    type("n1", "v1");
    await clock.advance(700);
    type("n1", "v2");
    await clock.advance(300);
    const [newer] = await adapter.list();
    assert.equal(newer.snapshot.body, "v2");
    server.ok(server.calls[0]);
    await clock.advance(1);
    const [kept] = await adapter.list();
    assert.equal(kept?.snapshot.body, "v2", "the ack for v1 left v2 in place");
    server.ok(server.calls[1]);
    await clock.advance(1);
    assert.deepEqual(await adapter.list(), []);
    assert.equal(saver.status("n1"), "clean");
  });

  test("offline: retried with backoff, never reverted, typing still journaled", async () => {
    const { clock, server, adapter, saver, events, type } = setup();
    saver.track("n1", { serverId: "n1", revision: 0 });
    type("n1", "draft");
    await clock.advance(700);
    server.fail(server.calls[0], 0);
    await clock.advance(1);
    assert.equal(saver.status("n1"), "offline");
    type("n1", "draft, more");
    await clock.advance(300);
    assert.equal((await adapter.list())[0].snapshot.body, "draft, more");
    await clock.advance(1000);
    const retry = server.calls.at(-1);
    assert.equal(retry.payload.body, "draft, more");
    server.ok(retry);
    await clock.advance(1);
    assert.equal(saver.status("n1"), "clean");
    assert.ok(events.some((event) => event.type === "saved"));
  });

  test("a conflict stops sending but keeps typing; resuming sends the newest on the new base", async () => {
    const { clock, server, adapter, saver, events, type } = setup();
    saver.track("n1", { serverId: "n1", revision: 2 });
    type("n1", "mine");
    await clock.advance(700);
    server.fail(server.calls[0], 409, { code: "conflict", reason: "stale", conflict_id: "c1" });
    await clock.advance(1);
    assert.equal(saver.status("n1"), "conflict");
    assert.equal(events.at(-1).detail.conflict_id, "c1");
    type("n1", "mine, continued");
    await clock.advance(5000);
    assert.equal(server.calls.length, 1, "nothing sent while blocked");
    assert.equal((await adapter.list())[0].snapshot.body, "mine, continued");
    saver.resume("n1", { id: "n1", revision: 7 });
    await clock.advance(1);
    assert.equal(server.calls.length, 2);
    assert.equal(server.calls[1].payload.base_revision, 7);
    assert.equal(server.calls[1].payload.body, "mine, continued");
  });

  test("a purged note blocks as gone", async () => {
    const { clock, server, saver, type } = setup();
    saver.track("n1", { serverId: "n1", revision: 0 });
    type("n1", "x");
    await clock.advance(700);
    server.fail(server.calls[0], 410, { code: "gone" });
    await clock.advance(1);
    assert.equal(saver.status("n1"), "gone");
  });

  test("a draft is created once; a replayed create is followed by the newer content", async () => {
    const { clock, server, saver, type } = setup();
    saver.track("draft-1", { folderId: "f1" });
    type("draft-1", "hello");
    await clock.advance(700);
    const create = server.calls[0];
    assert.equal(create.kind, "create");
    assert.equal(create.payload.write_id, "create:draft-1");
    assert.equal(create.payload.folder_id, "f1");
    create.resolve({ note: { id: "srv-1", revision: 0 }, replayed: true });
    await clock.advance(1);
    const follow = server.calls[1];
    assert.equal(follow.kind, "write");
    assert.equal(follow.id, "srv-1");
    assert.equal(follow.payload.body, "hello");
    assert.equal(saver.serverId("draft-1"), "srv-1");
  });

  test("a long burst is saved within three seconds", async () => {
    const { clock, server, saver, type } = setup();
    saver.track("n1", { serverId: "n1", revision: 0 });
    for (let i = 0; i < 10; i += 1) {
      type("n1", `text ${i}`);
      await clock.advance(400);
    }
    assert.ok(server.calls.length >= 1, "max wait forced a save during the burst");
  });

  test("capture freezes the note so the editor can move to another one", async () => {
    const { clock, server, saver } = setup();
    saver.track("a", { serverId: "a", revision: 0 });
    let live = "content of a";
    saver.edit("a", () => ({ title: "A", body: live, doc: {}, tags: [] }));
    saver.capture("a");
    live = "content of b";
    await clock.advance(700);
    assert.equal(server.calls[0].payload.body, "content of a");
  });

  test("persistAll writes every dirty note locally without sending", async () => {
    const { adapter, saver, type } = setup();
    saver.track("n1", { serverId: "n1" });
    type("n1", "unsent");
    await saver.persistAll();
    assert.equal((await adapter.list())[0].snapshot.body, "unsent");
  });
});

describe("locked notes in the saver", () => {
  function sealedSetup({ sealWorks = () => true } = {}) {
    const clock = fakeClock();
    const server = scriptedServer();
    const adapter = memoryJournalAdapter();
    const events = [];
    const saver = createNoteSaver({
      transport: server.transport,
      journal: createJournal(adapter, { tabId: "tab-1" }),
      tabId: "tab-1",
      timers: clock,
      now: clock.now,
      onEvent: (event) => events.push(event),
      project: (doc) => ({ title: `T ${doc.text}`, body: `body ${doc.text}`, tags: ["x"] }),
      seal: async (noteId, snapshot) => {
        if (!sealWorks()) throw new Error("keys are gone");
        // Opaque, as real ciphertext is: the words can't be read back out of it.
        return { title: `T`, cipher: { v: 1, iv: "iv", ct: Buffer.from(`${noteId}:${snapshot.doc.text}`).toString("base64") }, lock_key_version: 3, attachment_ids: ["f1"] };
      },
    });
    let text = "";
    const take = () => ({ doc: { type: "doc", text } });
    const type = (next) => {
      text = next;
      saver.edit("n1", take);
    };
    return { clock, server, adapter, saver, events, type };
  }

  test("only ciphertext is journaled and sent", async () => {
    const { clock, server, adapter, saver, type } = sealedSetup();
    saver.track("n1", { serverId: "n1", revision: 7, format: "encrypted" });
    type("gate code 4512");
    await clock.advance(300);
    const [entry] = await adapter.list();
    assert.equal(JSON.stringify(entry).includes("gate code"), false, "no plaintext in the journal");
    assert.equal(entry.snapshot.cipher.ct, Buffer.from("n1:gate code 4512").toString("base64"));
    await clock.advance(600);
    const [call] = server.calls;
    assert.equal(call.payload.format, "encrypted");
    assert.equal(call.payload.lock_key_version, 3);
    assert.equal(JSON.stringify(call.payload).includes("gate code"), false, "no plaintext sent");
    assert.equal("body" in call.payload || "doc" in call.payload || "tags" in call.payload, false);
  });

  test("when it can't be sealed, the edit is not kept anywhere as plaintext", async () => {
    let works = true;
    const { clock, server, adapter, saver, events, type } = sealedSetup({ sealWorks: () => works });
    saver.track("n1", { serverId: "n1", revision: 7, format: "encrypted" });
    works = false;
    type("secret words");
    await clock.advance(5000);
    assert.equal((await adapter.list()).length, 0);
    assert.equal(server.calls.length, 0);
    assert.ok(events.some((event) => event.type === "seal-error"));
  });

  test("after a lock, the queue moves to the sealed form and forgets the plaintext", async () => {
    const { clock, server, adapter, saver, type } = sealedSetup();
    saver.track("n1", { serverId: "n1", revision: 1 });
    type("plain before");
    await clock.advance(1000);
    server.ok(server.calls[0]);
    await clock.advance(10);
    // The lock committed at revision 3.
    saver.setFormat("n1", "encrypted", 3);
    saver.forgetPlaintext("n1");
    assert.equal(saver.latestSnapshot("n1"), null);
    type("after lock");
    await clock.advance(1000);
    const call = server.calls.at(-1);
    assert.equal(call.payload.format, "encrypted");
    assert.equal(call.payload.base_revision, 3);
    assert.equal(JSON.stringify(await adapter.list()).includes("after lock"), false);
  });

  test("a sealed journal entry replays as ciphertext, no key needed", async () => {
    const { clock, server, adapter, saver, type } = sealedSetup();
    saver.track("n1", { serverId: "n1", revision: 7, format: "encrypted" });
    type("words");
    await clock.advance(300);
    const replay = replayJournal({ journal: createJournal(adapter, { tabId: "tab-2" }), transport: server.transport, live: new Set(["tab-2"]) });
    await clock.advance(0);
    const call = server.calls.at(-1);
    assert.equal(call.payload.format, "encrypted");
    assert.equal(call.payload.cipher.ct, Buffer.from("n1:words").toString("base64"));
    server.ok(call);
    await replay;
  });
});

describe("recovery journal", () => {
  test("numbers rise per note across tabs; each tab keeps its own slot", async () => {
    const adapter = memoryJournalAdapter();
    const one = createJournal(adapter, { tabId: "t1" });
    const two = createJournal(adapter, { tabId: "t2" });
    assert.equal(await one.write("n", { snapshot: { body: "1" } }), 1);
    assert.equal(await two.write("n", { snapshot: { body: "2" } }), 2);
    assert.equal(await one.write("n", { snapshot: { body: "3" } }), 3);
    assert.equal(await one.write("other", { snapshot: {} }), 1);
    const slots = (await adapter.list()).filter((entry) => entry.noteKey === "n");
    assert.deepEqual(slots.map((entry) => [entry.tabId, entry.seq]).sort(), [["t1", 3], ["t2", 2]]);
  });

  test("acknowledging is compare-and-delete", async () => {
    const adapter = memoryJournalAdapter();
    const journal = createJournal(adapter, { tabId: "t1" });
    await journal.write("n", { snapshot: { body: "old" } });
    const seq = await journal.write("n", { snapshot: { body: "new" } });
    assert.equal(await journal.ack("n", seq - 1), false);
    assert.equal((await adapter.list()).length, 1);
    assert.equal(await journal.ack("n", seq), true);
    assert.equal((await adapter.list()).length, 0);
  });

  test("recovery takes this tab's slots and those of tabs that are gone", async () => {
    const adapter = memoryJournalAdapter();
    await createJournal(adapter, { tabId: "alive" }).write("a", { snapshot: {} });
    await createJournal(adapter, { tabId: "dead" }).write("b", { snapshot: {} });
    const mine = createJournal(adapter, { tabId: "me" });
    await mine.write("c", { snapshot: {} });
    const keys = (await mine.recoverable(new Set(["alive", "me"]))).map((entry) => entry.noteKey);
    assert.deepEqual(keys.sort(), ["b", "c"]);
    const withoutLocks = (await mine.recoverable(null)).map((entry) => entry.noteKey);
    assert.deepEqual(withoutLocks, ["c"], "without Web Locks only old slots count as abandoned");
  });

  test("replay sends with the original write id and settles each slot", async () => {
    const adapter = memoryJournalAdapter();
    const dead = createJournal(adapter, { tabId: "dead" });
    await dead.write("n1", { serverId: "n1", baseRevision: 3, snapshot: { title: "a", body: "a" } });
    await dead.write("n2", { serverId: "n2", baseRevision: 1, snapshot: { body: "b" } });
    await dead.write("draft-9", { serverId: null, folderId: null, snapshot: { body: "d" } });
    await dead.write("n3", { serverId: "n3", baseRevision: 0, snapshot: { body: "gone" } });
    const sent = [];
    const transport = {
      async create(payload) {
        sent.push(["create", payload.write_id]);
        return { note: { id: "srv-9", revision: 0 }, replayed: true };
      },
      async write(id, payload) {
        sent.push(["write", id, payload.write_id, payload.base_revision]);
        if (id === "n2") throw Object.assign(new Error("conflict"), { status: 409, detail: { code: "conflict" } });
        if (id === "n3") throw Object.assign(new Error("gone"), { status: 410, detail: { code: "gone" } });
        return { note: { id, revision: payload.base_revision + 1 } };
      },
    };
    const me = createJournal(adapter, { tabId: "me" });
    const outcomes = await replayJournal({ journal: me, transport, live: new Set(["me"]) });
    assert.deepEqual(outcomes.map((item) => [item.entry.noteKey, item.result]), [
      ["n1", "saved"],
      ["n2", "conflict"],
      ["draft-9", "saved"],
      ["n3", "gone"],
    ]);
    assert.deepEqual(sent[0], ["write", "n1", "dead:n1:1", 3]);
    assert.deepEqual(sent[2], ["create", "create:draft-9"]);
    assert.deepEqual(sent[3], ["write", "srv-9", "dead:draft-9:1", 0]);
    const left = (await adapter.list()).map((entry) => entry.noteKey);
    assert.deepEqual(left, ["n3"], "only the purged note's content is kept, for Save as new note");
  });

  test("a replayed entry that holds only its document is sent with its projection", async () => {
    const adapter = memoryJournalAdapter();
    await createJournal(adapter, { tabId: "gone" }).write("n1", { serverId: "n1", baseRevision: 2, snapshot: { doc: { type: "doc", text: "hi" } } });
    const sent = [];
    const transport = {
      async write(id, payload) {
        sent.push(payload);
        return { note: { id, revision: 3 } };
      },
    };
    await replayJournal({
      journal: createJournal(adapter, { tabId: "me" }),
      transport,
      live: new Set(["me"]),
      project: (doc) => ({ title: "Hi", body: doc.text, tags: [] }),
    });
    assert.equal(sent.length, 1);
    assert.equal(sent[0].title, "Hi");
    assert.equal(sent[0].body, "hi");
  });

  test("replay stops at the first network failure and keeps everything after it", async () => {
    const adapter = memoryJournalAdapter();
    const dead = createJournal(adapter, { tabId: "dead" });
    await dead.write("n1", { serverId: "n1", snapshot: {} });
    await dead.write("n2", { serverId: "n2", snapshot: {} });
    const transport = {
      async write() {
        throw new Error("Backend API is not reachable.");
      },
    };
    const outcomes = await replayJournal({
      journal: createJournal(adapter, { tabId: "me" }),
      transport,
      live: new Set(["me"]),
    });
    assert.deepEqual(outcomes.map((item) => item.result), ["deferred"]);
    assert.equal((await adapter.list()).length, 2);
  });
});
