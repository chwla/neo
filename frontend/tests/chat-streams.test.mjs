/**
 * One stream, many conversations.
 *
 * Neo answers several chats at once over a single connection, so the reader has
 * to keep each one's state apart from the others. These pin that separation --
 * and, just as importantly, that folding a record into one chat's state is still
 * exactly what it was when there was only ever one chat.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { IDLE, applyEvent, reduce } from "../src/chatStream.js";
import BackgroundTurnToast, {
  notificationsEnabled,
  shouldNotify,
} from "../src/BackgroundTurnToast.jsx";
import { createSendGuard } from "../src/sendGuard.js";

const fold = (events, start = new Map()) => events.reduce(applyEvent, start);

describe("demultiplexing one tail into many chats", () => {
  test("two chats' records interleave without touching each other", () => {
    const streams = fold([
      { chat_id: 1, type: "chunk", content: "one ", generation_id: "g1", seq: 1 },
      { chat_id: 2, type: "chunk", content: "two ", generation_id: "g2", seq: 2 },
      { chat_id: 1, type: "chunk", content: "more", generation_id: "g1", seq: 3 },
      { chat_id: 2, type: "chunk", content: "also", generation_id: "g2", seq: 4 },
    ]);
    assert.equal(streams.get(1).text, "one more");
    assert.equal(streams.get(2).text, "two also");
  });

  test("a chat finishing leaves every other chat exactly as it was", () => {
    // The failure this exists for: a background chat completing used to clear
    // the state of whichever chat the user was actually reading.
    const before = fold([
      { chat_id: 1, type: "chunk", content: "still typing", generation_id: "g1", seq: 1 },
      { chat_id: 2, type: "chunk", content: "done soon", generation_id: "g2", seq: 2 },
    ]);
    const after = applyEvent(before, { chat_id: 2, type: "run.completed", seq: 3 });
    assert.equal(after.get(1), before.get(1), "the untouched chat keeps its identity");
    assert.equal(after.get(1).text, "still typing");
  });

  test("a record with no chat cannot be routed, so it is ignored", () => {
    const streams = new Map();
    assert.equal(applyEvent(streams, { type: "chunk", content: "x" }), streams);
  });

  test("connection bookkeeping is not conversation state", () => {
    // `cursor` and `idle` describe the stream, not any chat. Folding them in
    // would create a phantom entry and, with it, a phantom badge.
    const streams = new Map();
    assert.equal(applyEvent(streams, { type: "cursor", seq: 12 }), streams);
    assert.equal(applyEvent(streams, { type: "idle", seq: 12, chat_id: 3 }), streams);
    assert.equal(streams.size, 0);
  });

  test("an unchanged fold returns the same Map, so React can skip the render", () => {
    const streams = new Map();
    assert.equal(applyEvent(streams, { type: "idle", seq: 1 }), streams);
  });
});

describe("reduce still says what it always said", () => {
  // The refactor's central claim is that per-chat state reuses this untouched.
  // Asserting the whole table is what makes that claim checkable rather than
  // hopeful.
  const from = (events) => events.reduce(reduce, IDLE);

  test("chunks accumulate and replace restates", () => {
    assert.equal(from([{ type: "chunk", content: "a" }, { type: "chunk", content: "b" }]).text, "ab");
    assert.equal(
      from([{ type: "chunk", content: "draft" }, { type: "replace", content: "final" }]).text,
      "final",
    );
  });

  test("thinking accumulates separately from the answer", () => {
    const state = from([{ type: "thinking", content: "hm" }, { type: "chunk", content: "hi" }]);
    assert.equal(state.thinking, "hm");
    assert.equal(state.text, "hi");
  });

  test("status text and todos are carried", () => {
    assert.equal(from([{ type: "run.status", content: "Searching" }]).statusText, "Searching");
    assert.deepEqual(from([{ type: "todo.updated", items: [{ text: "a" }] }]).todo, [{ text: "a" }]);
  });

  test("an approval normalises its id and stands until the run moves on", () => {
    const waiting = from([
      { type: "approval.required", approval_id: "ap1", agent_session_id: "s1" },
    ]);
    assert.equal(waiting.approval.id, "ap1");
    assert.equal(waiting.sessionStatus, "waiting_approval");
    assert.equal(reduce(waiting, { type: "run.status", content: "on" }).approval, null);
  });

  test("a new turn clears the last one rather than printing both", () => {
    const first = from([{ type: "chunk", content: "old", generation_id: "g1" }]);
    const second = reduce(first, { type: "chunk", content: "new", generation_id: "g2" });
    assert.equal(second.text, "new");
  });

  test("an unknown record changes nothing", () => {
    const state = from([{ type: "chunk", content: "a" }]);
    assert.equal(reduce(state, { type: "something.new" }).text, "a");
  });

  test("a queued turn says so, and starting overwrites it", () => {
    const queued = from([{ type: "turn.queued", generation_id: "g1" }]);
    assert.equal(queued.sessionStatus, "queued");
    assert.match(queued.statusText, /Queued/);
    assert.equal(reduce(queued, { type: "run.started" }).sessionStatus, "running");
  });
});

describe("send guards are per chat", () => {
  test("one chat's in-flight send does not refuse another chat's", () => {
    const guards = new Map();
    const guardFor = (id) => {
      if (!guards.has(id)) guards.set(id, createSendGuard());
      return guards.get(id);
    };
    assert.ok(guardFor(1).begin(), "the first chat claims its slot");
    assert.ok(guardFor(2).begin(), "a different chat is not blocked by it");
  });

  test("a double click within one chat is still refused", () => {
    const guard = createSendGuard();
    assert.ok(guard.begin());
    assert.equal(guard.begin(), null);
    guard.release();
    assert.ok(guard.begin(), "and the slot is reusable once released");
  });
});

describe("who gets told about a finished turn", () => {
  test("not the chat being watched", () => {
    assert.equal(shouldNotify({ chatId: 5, visibleChatId: 5, hidden: false }), false);
  });

  test("but yes when the tab is hidden, even for that chat", () => {
    assert.equal(shouldNotify({ chatId: 5, visibleChatId: 5, hidden: true }), true);
  });

  test("yes for a chat that is not on screen", () => {
    assert.equal(shouldNotify({ chatId: 5, visibleChatId: 9, hidden: false }), true);
  });

  test("yes when another view is up, which reads as no visible chat", () => {
    assert.equal(shouldNotify({ chatId: 5, visibleChatId: null, hidden: false }), true);
  });

  test("a turn with no chat is never announced", () => {
    assert.equal(shouldNotify({ chatId: null, visibleChatId: 9, hidden: true }), false);
  });

  test("desktop notifications stay off unless switched on, storage or not", () => {
    assert.equal(notificationsEnabled({ getItem: () => null }), false);
    assert.equal(notificationsEnabled({ getItem: () => "1" }), true);
    assert.equal(
      notificationsEnabled({
        getItem() {
          throw new Error("private mode");
        },
      }),
      false,
    );
  });
});

describe("the finished-turn toast", () => {
  const render = (props) =>
    renderToStaticMarkup(
      createElement(BackgroundTurnToast, {
        chatTitles: new Map([[7, "Refactor the parser"]]),
        onOpen: () => {},
        onDismiss: () => {},
        ...props,
      }),
    );

  test("nothing is drawn when nothing has finished", () => {
    assert.equal(render({ notices: [] }), "");
  });

  test("it names the chat and what happened to it", () => {
    const html = render({ notices: [{ id: 1, chatId: 7, outcome: "run.completed" }] });
    assert.match(html, /Refactor the parser/);
    assert.match(html, /Finished replying/);
  });

  test("a failure reads differently from a success", () => {
    const html = render({ notices: [{ id: 2, chatId: 7, outcome: "run.failed" }] });
    assert.match(html, /is-failed/);
    assert.match(html, /Stopped with an error/);
  });

  test("a chat the sidebar has not named still gets a toast", () => {
    const html = render({ notices: [{ id: 3, chatId: 99, outcome: "run.completed" }] });
    assert.match(html, /Chat/);
  });
});

describe("badge precedence", () => {
  // Mirrors the server's _STATUS_PRECEDENCE. A chat holding more than one
  // unfinished thing should show the one the user has to act on.
  const RANK = ["waiting_approval", "running", "queued"];
  const loudest = (...states) =>
    states.filter(Boolean).sort((a, b) => RANK.indexOf(a) - RANK.indexOf(b))[0] ?? null;

  test("an approval outranks a run, which outranks a queued turn", () => {
    assert.equal(loudest("running", "waiting_approval"), "waiting_approval");
    assert.equal(loudest("queued", "running"), "running");
    assert.equal(loudest("queued"), "queued");
    assert.equal(loudest(), null);
  });

  test("live stream state is preferred over the stored sidebar value", () => {
    // The stream is up to a poll interval fresher than a refetch; reading the
    // stored value first is what made badges lag the work they describe.
    const statusFor = (chat, streams) =>
      streams.get(chat.id)?.sessionStatus ??
      (streams.get(chat.id)?.kind ? "running" : null) ??
      chat.turn_status ??
      null;
    const streams = new Map([[1, { sessionStatus: "running" }]]);
    assert.equal(statusFor({ id: 1, turn_status: "queued" }, streams), "running");
    assert.equal(statusFor({ id: 2, turn_status: "queued" }, streams), "queued");
  });

  test("a queued turn reaching the reducer shows as queued", () => {
    const streams = fold([{ chat_id: 4, type: "turn.queued", generation_id: "g" }]);
    assert.equal(streams.get(4).sessionStatus, "queued");
  });
});

describe("the optimistic-message race", () => {
  /**
   * The failure this guards: a send in A awaits, the user switches to B, and the
   * response comes back. Turn state is keyed by chat so it is always safe to
   * write; transcript edits are not, because only one transcript is loaded.
   */
  function applyResult({ sentFrom, visibleNow, turns, messages }) {
    const nextTurns = new Map(turns);
    nextTurns.set(sentFrom, { kind: "chat", generationId: "g1" });
    const nextMessages =
      visibleNow === sentFrom ? [...messages, { chat_id: sentFrom, role: "assistant" }] : messages;
    return { turns: nextTurns, messages: nextMessages };
  }

  test("the turn is recorded against the chat it was sent from", () => {
    const out = applyResult({ sentFrom: 1, visibleNow: 2, turns: new Map(), messages: [] });
    assert.ok(out.turns.has(1));
    assert.equal(out.turns.has(2), false, "never against the chat now on screen");
  });

  test("no message is written into the transcript the user switched to", () => {
    const out = applyResult({ sentFrom: 1, visibleNow: 2, turns: new Map(), messages: [] });
    assert.deepEqual(out.messages, []);
  });

  test("but it is written when the user never left", () => {
    const out = applyResult({ sentFrom: 1, visibleNow: 1, turns: new Map(), messages: [] });
    assert.equal(out.messages.length, 1);
  });
});

describe("cancellation and completion stay in their own chat", () => {
  test("one chat's terminal event leaves the others' text intact", () => {
    const streams = fold([
      { chat_id: 1, type: "chunk", content: "A works", generation_id: "g1" },
      { chat_id: 2, type: "chunk", content: "B works", generation_id: "g2" },
      { chat_id: 3, type: "chunk", content: "C works", generation_id: "g3" },
      { chat_id: 2, type: "run.cancelled", generation_id: "g2" },
    ]);
    assert.equal(streams.get(1).text, "A works");
    assert.equal(streams.get(3).text, "C works");
  });

  test("tokens never cross between chats, however interleaved", () => {
    const streams = fold(
      Array.from({ length: 30 }, (_, i) => ({
        chat_id: (i % 3) + 1,
        type: "chunk",
        content: String(i % 3),
        generation_id: `g${(i % 3) + 1}`,
      })),
    );
    assert.equal(streams.get(1).text, "0".repeat(10));
    assert.equal(streams.get(2).text, "1".repeat(10));
    assert.equal(streams.get(3).text, "2".repeat(10));
  });
});

describe("the stream connection", () => {
  test("a reconnect resumes from the cursor rather than replaying", () => {
    // What the hook tracks across a dropped connection.
    let cursor = null;
    const advance = (event) => {
      if (event.type === "cursor") cursor = event.seq;
      else cursor = Math.max(cursor ?? 0, event.seq || 0);
    };
    [
      { type: "cursor", seq: 100 },
      { chat_id: 1, type: "chunk", seq: 101 },
      { chat_id: 2, type: "chunk", seq: 102 },
    ].forEach(advance);
    assert.equal(cursor, 102, "the next connection asks for everything after 102");
  });

  test("an idle close does not lose the cursor", () => {
    let cursor = 7;
    const event = { type: "idle", seq: 9 };
    cursor = Math.max(cursor, event.seq || 0);
    assert.equal(cursor, 9);
  });

  test("the cursor never goes backwards on an out-of-order record", () => {
    let cursor = 50;
    cursor = Math.max(cursor, 12);
    assert.equal(cursor, 50);
  });
});
