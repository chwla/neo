/**
 * The dictation button in the composer, rendered for real and read back as markup.
 *
 * It shares one slot with send, and the rule deciding which of them is in it is the
 * thing worth pinning: empty composer dictates, the first character worth sending
 * swaps it, a recording in progress outranks both so it can be stopped, and a turn in
 * flight outranks everything. Both modes render their own composer chrome, so the rule
 * is checked in each. The listening bar below is separate and stays separate -- it
 * carries the elapsed time, the input level and Cancel, none of which fit in 40px.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { ChatComposer } from "../src/App.jsx";

const READY = { available: true, reason: "ready", message: "Ready." };

function render(overrides = {}) {
  const props = {
    disabled: false,
    value: "",
    onChange() {},
    onSubmit() {},
    llms: [{ id: "l1", name: "Local", model: "qwen3", enabled: true }],
    llmId: "l1",
    onLlmChange() {},
    mode: "chat",
    onModeChange() {},
    repos: [],
    selectedRepoId: "",
    onRepoChange() {},
    // Agent mode renders its own set of chips and needs these; the chat branch
    // ignores them.
    agentDefinitions: [{ id: "builtin-general", name: "general", display_name: "General" }],
    selectedAgentDefinitionId: "general",
    onAgentDefinitionChange() {},
    agentMode: "normal",
    onAgentModeChange() {},
    agentMessage: "",
    voice: READY,
    dictation: null,
    ...overrides,
  };
  return renderToStaticMarkup(createElement(ChatComposer, props));
}

const count = (haystack, needle) => haystack.split(needle).length - 1;

describe("the dictation button", () => {
  test("holds the send slot while the composer is empty, in chat mode", () => {
    const html = render({ mode: "chat", value: "" });
    assert.equal(count(html, "chat-dictate-button"), 1);
    assert.ok(!html.includes('aria-label="Send message"'), "nothing to send, so no send button");
  });

  test("holds it in agent mode too", () => {
    // Each mode renders its own composer chrome; a control wired into one branch
    // only is missing from the other, which is how the old menu entry was built.
    const html = render({ mode: "agent", value: "" });
    assert.equal(count(html, "chat-dictate-button"), 1);
    assert.ok(!html.includes('aria-label="Send message"'));
  });

  for (const mode of ["chat", "agent"]) {
    test(`the first character worth sending takes the slot back in ${mode} mode`, () => {
      const html = render({ mode, value: "h" });
      assert.ok(html.includes('aria-label="Send message"'));
      assert.equal(count(html, "chat-dictate-button"), 0, "two controls cannot share one slot");
    });
  }

  test("whitespace alone is not something to send", () => {
    // `handleSendMessage` trims before deciding, so a lone space would otherwise
    // swap in a send button that refuses to send.
    const html = render({ value: "   " });
    assert.equal(count(html, "chat-dictate-button"), 1);
    assert.ok(!html.includes('aria-label="Send message"'));
  });

  test("a recording in progress keeps the slot whatever is in the field", () => {
    // Text can be in the composer before dictation starts, or land there when an
    // earlier take transcribes. Handing the slot to send at that moment would leave
    // the recording running with no button to stop it.
    const html = render({
      value: "already typed this",
      dictation: { recording: true, busy: false, seconds: 3, level: 0.1, stop() {}, cancel() {} },
    });
    assert.ok(html.includes("chat-dictate-button"));
    assert.ok(html.includes("is-recording"));
    assert.ok(html.includes('aria-label="Stop dictation"'));
    assert.ok(!html.includes('aria-label="Send message"'));
  });

  test("transcribing holds the slot as well, and cannot be pressed", () => {
    const html = render({ value: "", dictation: { recording: false, busy: true } });
    assert.ok(/chat-dictate-button[^>]*disabled/.test(html), "there is nothing to start or stop");
    assert.ok(html.includes('aria-label="Transcribing…"'));
  });

  test("a turn in flight outranks both", () => {
    const html = render({ value: "", generating: true, onStop() {} });
    assert.ok(html.includes("stop-button"));
    assert.equal(count(html, "chat-dictate-button"), 0, "the microphone cannot stop a turn");
  });

  test("is gone from the + menu it used to live in", () => {
    // Speaking is one of the two ways to fill the composer, not a thing you do once
    // a conversation, so it does not belong in a popover with those.
    for (const mode of ["chat", "agent"]) {
      const html = render({ mode, value: "typed, so the button is send" });
      assert.ok(!html.includes("chat-dictate-button"), `${mode} mode still has a menu entry`);
    }
  });

  test("stays reachable, with the reason, when speech recognition is not installed", () => {
    const html = render({
      voice: {
        available: false,
        reason: "dependency_missing",
        message: "Speech recognition is not installed.",
      },
    });
    assert.ok(html.includes("chat-dictate-button"), "still rendered, so the reason is reachable");
    assert.ok(html.includes("Speech recognition is not installed."), "the title carries it");
    // Enabled: this state is a task, and the button routes to the settings screen.
    assert.ok(!/chat-dictate-button[^>]*disabled/.test(html));
  });

  test("offers set-up rather than recording when the model is missing", () => {
    const html = render({
      voice: { available: false, reason: "model_not_downloaded", message: "Not downloaded." },
    });
    // The name stays "Dictate" and the missing piece is named after it, so the button
    // reads as the thing you want with a prerequisite rather than as a different
    // feature. There is no visible label at 40px square, so the accessible name is
    // where both halves have to live.
    assert.ok(html.includes('aria-label="Dictate, Download voice model"'));
  });
});

describe("the listening bar", () => {
  test("is absent while idle", () => {
    const html = render({ dictation: { recording: false, busy: false } });
    assert.ok(!html.includes("composer-dictation-dot"));
  });

  test("shows the timer and a way to stop while recording", () => {
    const html = render({
      dictation: { recording: true, busy: false, seconds: 72, level: 0.1, stop() {}, cancel() {} },
    });
    assert.ok(html.includes("composer-dictation-dot"), "visible outside the popover");
    assert.ok(html.includes("1:12"), "elapsed time, m:ss");
    assert.ok(html.includes("Stop"));
    assert.ok(html.includes("Cancel"));
  });

  test("says it is transcribing once recording has stopped", () => {
    const html = render({ dictation: { recording: false, busy: true } });
    assert.ok(html.includes("Transcribing…"));
    assert.ok(!html.includes("composer-dictation-stop"), "nothing left to stop");
  });

  test("surfaces an error with its remedy", () => {
    const html = render({
      dictation: {
        recording: false,
        busy: false,
        error: { message: "Neo needs permission to use your microphone.", remedy: "Allow it." },
        dismissError() {},
      },
    });
    assert.ok(html.includes("Neo needs permission to use your microphone."));
    assert.ok(html.includes("Allow it."));
  });
});

describe("the Dictate button never leads nowhere", () => {
  const cases = [
    ["dependency_missing", "Voice setup required"],
    ["model_not_downloaded", "Download voice model"],
    ["model_downloading", "Downloading voice model…"],
    ["disabled", "Turn on voice input"],
  ];

  for (const [reason, hint] of cases) {
    test(`${reason} offers "${hint}" and stays clickable`, () => {
      const html = render({ voice: { available: false, reason, message: "…" } });
      assert.ok(html.includes(hint), "the hint explains what is missing");
      // Enabled, because every one of these states is something the user can fix.
      assert.ok(
        !/chat-dictate-button[^>]*disabled/.test(html),
        `${reason} must route to setup rather than being greyed out`,
      );
    });
  }

  test("a browser that cannot record at all is the one inert case", () => {
    const html = render({
      voice: { available: false, reason: "unsupported_context", message: "No microphone here." },
    });
    assert.ok(/chat-dictate-button[^>]*disabled/.test(html));
  });

  test("the hint is part of the accessible name, not just decoration", () => {
    const html = render({ voice: { available: false, reason: "dependency_missing", message: "…" } });
    assert.ok(html.includes('aria-label="Dictate, Voice setup required"'));
  });
});
