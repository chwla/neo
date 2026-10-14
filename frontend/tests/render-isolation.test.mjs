/**
 * Keeping unrelated parts of the app from re-rendering.
 *
 * The sidebar and the composer used to re-render on every keystroke, every
 * streamed frame and every dictation level tick, because everything above them
 * re-rendered and nothing stopped it. Three things stop it now, and each is
 * pinned here: the sidebar's status function changes only when a status does,
 * the dictation meter lives in a store only the meter reads, and the big
 * components are memoized.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { ChatComposer, PendingAssistantMessage, Sidebar } from "../src/App.jsx";
import { chatStatusKey, createStatusFor } from "../src/chatStatus.js";
import { createMeter } from "../src/voice/meter.js";

/** The function the sidebar was handed before, kept as the reference. */
function referenceStatusFor(streams, turns, finishedChatIds) {
  return (chat) => {
    const liveState = streams.get(chat.id);
    if (liveState?.sessionStatus) return liveState.sessionStatus;
    if (liveState?.kind) return "running";
    if (turns.has(chat.id)) return "running";
    const stored = chat.turn_status || chat.agent_status;
    if (stored) return stored;
    return finishedChatIds.has(chat.id) ? "done" : null;
  };
}

describe("the sidebar's status function", () => {
  const liveStates = [undefined, { kind: "chat", text: "hi" }, { kind: "agent", sessionStatus: "waiting_approval" }, {}];
  const chats = [
    { id: 1 }, { id: 2, turn_status: "queued" }, { id: 3, agent_status: "failed" }, { id: 4 },
  ];

  test("answers exactly as before, for every combination", () => {
    for (const live of liveStates) {
      for (const tracked of [false, true]) {
        for (const finished of [false, true]) {
          const streams = new Map(live ? [[1, live], [2, live], [3, live]] : []);
          const turns = new Map(tracked ? [[1, {}], [4, {}]] : []);
          const done = new Set(finished ? [3, 4] : []);
          const ours = createStatusFor(streams, turns, done);
          const theirs = referenceStatusFor(streams, turns, done);
          for (const chat of chats) assert.equal(ours(chat), theirs(chat), JSON.stringify({ live, tracked, finished, chat }));
        }
      }
    }
  });

  test("its key moves with a status, not with streamed text", () => {
    const turns = new Map([[1, {}]]);
    const done = new Set();
    const before = chatStatusKey(new Map([[1, { kind: "chat", text: "Hel" }]]), turns, done);
    const moreText = chatStatusKey(new Map([[1, { kind: "chat", text: "Hello there" }]]), turns, done);
    assert.equal(before, moreText, "a token that changes no badge keeps the key");
    const waiting = chatStatusKey(new Map([[1, { kind: "agent", sessionStatus: "waiting_approval" }]]), turns, done);
    assert.notEqual(before, waiting);
    assert.notEqual(chatStatusKey(new Map(), new Map(), new Set([5])), chatStatusKey(new Map(), new Map(), new Set()));
  });

  test("its key does not depend on insertion order", () => {
    const a = chatStatusKey(new Map([[1, { kind: "chat" }], [2, { kind: "chat" }]]), new Map([[3, {}], [4, {}]]), new Set([5, 6]));
    const b = chatStatusKey(new Map([[2, { kind: "chat" }], [1, { kind: "chat" }]]), new Map([[4, {}], [3, {}]]), new Set([6, 5]));
    assert.equal(a, b);
  });
});

describe("the dictation meter store", () => {
  test("hands back the same snapshot until a value changes", () => {
    const meter = createMeter();
    const first = meter.getSnapshot();
    meter.set({ level: 0 });
    assert.equal(meter.getSnapshot(), first, "an unchanged store must not read as an update");
    meter.set({ level: 0.4 });
    assert.notEqual(meter.getSnapshot(), first);
    assert.deepEqual(meter.getSnapshot(), { level: 0.4, seconds: 0 });
    assert.ok(Object.isFrozen(meter.getSnapshot()));
  });

  test("tells subscribers only about changes, and lets them leave", () => {
    const meter = createMeter();
    let calls = 0;
    const unsubscribe = meter.subscribe(() => { calls += 1; });
    meter.set({ seconds: 1 });
    meter.set({ seconds: 1 });
    meter.set({ level: 0.2 });
    assert.equal(calls, 2);
    unsubscribe();
    meter.set({ seconds: 2 });
    assert.equal(calls, 2);
  });

  test("resets to zero once", () => {
    const meter = createMeter();
    meter.set({ level: 0.3, seconds: 9 });
    let calls = 0;
    meter.subscribe(() => { calls += 1; });
    meter.reset();
    meter.reset();
    assert.deepEqual(meter.getSnapshot(), { level: 0, seconds: 0 });
    assert.equal(calls, 1);
  });

  test("keeps subscribe and getSnapshot stable, as React requires", () => {
    const meter = createMeter();
    const { subscribe, getSnapshot } = meter;
    meter.set({ level: 0.5 });
    assert.equal(meter.subscribe, subscribe);
    assert.equal(meter.getSnapshot, getSnapshot);
  });
});

describe("the composer's meter", () => {
  test("reads the clock and level from the store", () => {
    const meter = createMeter();
    meter.set({ seconds: 72, level: 0.1 });
    const html = renderToStaticMarkup(createElement(ChatComposer, {
      value: "", onChange() {}, onSubmit() {}, llms: [], llmId: "", onLlmChange() {},
      mode: "chat", onModeChange() {}, agentDefinitions: [], selectedAgentDefinitionId: "general",
      onAgentDefinitionChange() {}, onRepoChange() {}, onAgentModeChange() {}, agentMessage: "",
      voice: { available: true, ready: true },
      dictation: { recording: true, busy: false, meter, stop() {}, cancel() {} },
    }));
    assert.match(html, /composer-dictation-time">1:12</);
    assert.match(html, /--level:0\.6/);
  });
});

describe("memo boundaries", () => {
  const MEMO = Symbol.for("react.memo");
  for (const [name, component] of Object.entries({ Sidebar, ChatComposer, PendingAssistantMessage })) {
    test(`${name} is memoized`, () => {
      assert.equal(component.$$typeof, MEMO, `${name} lost its memo wrapper`);
    });
  }
});
