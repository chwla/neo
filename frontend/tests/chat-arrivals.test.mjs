/**
 * Which transcript rows slide in.
 *
 * Only content that is new to the person looking animates, and only the code
 * that creates it says so: the bubble just sent, the reply it starts, an agent
 * run it starts. A saved row is never news -- not on a chat switch, not after a
 * trip to a workspace, not when it replaces the pending row it stood in for --
 * so those mount still, on the frame they are asked for.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { groupEntries } from "../src/AgentTurn.jsx";
import { ChatTranscript, PendingAssistantMessage } from "../src/App.jsx";
import {
  agentArrivalKey,
  arrivalKeyFor,
  createArrivals,
  replyArrivalKey,
} from "../src/chatPresentation.js";

const transcriptProps = (messages, arrivals) => ({
  messages, contextWindowIndex: {}, sessionTokensUsed: 0, editingMessageId: null, editingValue: "",
  openThinkingMessageId: null, agentRuns: {}, agentBusy: false, agentPatch: "", agentPatchSessionId: null,
  actions: { onSetEditingValue() {} }, arrivals,
});

const rowsMarked = (html) => (html.match(/neo-chat-message[^"]*is-arriving/g) || []).length;

describe("the arrival registry", () => {
  test("marks, answers and consumes keys", () => {
    const arrivals = createArrivals();
    arrivals.mark("message:pending-1");
    assert.equal(arrivals.has("message:pending-1"), true);
    arrivals.consume("message:pending-1");
    assert.equal(arrivals.has("message:pending-1"), false);
  });

  test("stays bounded when marks are never consumed", () => {
    const arrivals = createArrivals(3);
    for (const key of ["a", "b", "c", "d", "e"]) arrivals.mark(key);
    assert.equal(arrivals.size, 3);
    assert.equal(arrivals.has("a"), false, "the oldest marks go first");
    assert.equal(arrivals.has("e"), true);
  });

  test("keys rows by their run when they are agent turns, by id otherwise", () => {
    assert.equal(arrivalKeyFor({ id: "pending-7" }), "message:pending-7");
    assert.equal(arrivalKeyFor({ id: 42, agent: { session: { id: "s-1" } } }), agentArrivalKey("s-1"));
    assert.equal(replyArrivalKey(5, 1000), "turn:5:1000");
  });
});

describe("transcript rows", () => {
  const saved = [
    { id: 1, role: "user", content: "Saved question" },
    { id: 2, role: "assistant", content: "Saved answer" },
  ];

  test("a just-sent bubble slides in; saved rows beside it do not", () => {
    const arrivals = createArrivals();
    const pending = { id: "pending-9", role: "user", content: "Just sent" };
    arrivals.mark(arrivalKeyFor(pending));
    const html = renderToStaticMarkup(createElement(ChatTranscript, transcriptProps([...saved, pending], arrivals)));
    assert.equal(rowsMarked(html), 1);
    assert.match(html, /is-arriving[\s\S]*Just sent/);
  });

  test("a saved row is never news, even with the same content as a pending one", () => {
    const arrivals = createArrivals();
    arrivals.mark("message:pending-9");
    // The saved copy that replaces the pending bubble has a key nobody marked.
    const html = renderToStaticMarkup(createElement(ChatTranscript, transcriptProps(
      [...saved, { id: 3, role: "user", content: "Just sent" }], arrivals,
    )));
    assert.equal(rowsMarked(html), 0);
  });

  test("an agent run's row slides in when its run was marked", () => {
    const arrivals = createArrivals();
    arrivals.mark(agentArrivalKey("s-9"));
    const agentRow = { id: 4, role: "assistant", content: "", response_kind: "agent_run", agent: { session: { id: "s-9" } } };
    const html = renderToStaticMarkup(createElement(ChatTranscript, transcriptProps([...saved, agentRow], arrivals)));
    assert.equal(rowsMarked(html), 1);
  });

  test("without a registry nothing animates", () => {
    const html = renderToStaticMarkup(createElement(ChatTranscript, transcriptProps(
      [...saved, { id: "pending-1", role: "user", content: "x" }], undefined,
    )));
    assert.equal(rowsMarked(html), 0);
  });
});

describe("the pending reply", () => {
  const generation = { rawContent: "", content: "", thinking: "", statusDetail: "Waiting" };

  test("slides in when the turn that started it was marked", () => {
    const arrivals = createArrivals();
    const key = replyArrivalKey(5, 1000);
    arrivals.mark(key);
    const html = renderToStaticMarkup(createElement(PendingAssistantMessage, {
      generation, startedAt: 1000, arrivals, arrivalKey: key,
    }));
    assert.match(html, /neo-chat-message assistant thinking is-arriving/);
  });

  test("stays still for a turn resumed after a reload", () => {
    const html = renderToStaticMarkup(createElement(PendingAssistantMessage, {
      generation, startedAt: 1000, arrivals: createArrivals(), arrivalKey: replyArrivalKey(5, 1000),
    }));
    assert.doesNotMatch(html, /is-arriving/);
  });
});

describe("agent narration bubbles", () => {
  test("keep the first chunk's id as more chunks merge in", () => {
    const merged = groupEntries([
      { kind: "text", id: 11, content: "Hel" },
      { kind: "text", id: 12, content: "lo" },
      { kind: "text", id: 13, content: "!" },
      { kind: "tool", id: "call-1" },
      { kind: "text", id: 15, content: "Done" },
    ]);
    assert.deepEqual(merged.map((entry) => entry.id), [11, "call-1", 15]);
    assert.equal(merged[0].content, "Hello!");
  });
});
