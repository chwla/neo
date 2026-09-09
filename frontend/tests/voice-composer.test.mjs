/**
 * The dictation entry in the composer, rendered for real and read back as markup.
 *
 * Two things are pinned here that a refactor would otherwise quietly break. The entry
 * has to appear in *both* menu branches -- agent mode and chat mode render separate
 * sets of actions -- and its state has to be visible from outside the popover it lives
 * in, because a status you can only see by reopening a menu is not a status.
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

describe("the dictation entry", () => {
  test("appears in chat mode", () => {
    assert.equal(count(render({ mode: "chat" }), "chat-dictate-button"), 1);
  });

  test("appears in agent mode too", () => {
    // The menu renders a separate set of actions per mode; an entry added to only
    // one branch is invisible in the other.
    assert.equal(count(render({ mode: "agent" }), "chat-dictate-button"), 1);
  });

  test("is disabled with the reason when speech recognition is not installed", () => {
    const html = render({
      voice: {
        available: false,
        reason: "dependency_missing",
        message: "Speech recognition is not installed.",
      },
    });
    assert.ok(html.includes("chat-dictate-button"), "still rendered, so the reason is reachable");
    assert.ok(html.includes("Speech recognition is not installed."));
    assert.ok(/chat-dictate-button[^>]*disabled/.test(html) || html.includes("disabled=\"\""));
  });

  test("offers set-up rather than recording when the model is missing", () => {
    const html = render({
      voice: { available: false, reason: "model_not_downloaded", message: "Not downloaded." },
    });
    // The label stays "Dictate" and a second line says what is missing, so the entry
    // reads as the thing you want with a prerequisite rather than as a different
    // feature. An unset-up capability is a task, not a choice.
    assert.ok(html.includes("Download voice model"));
    assert.ok(html.includes(">Dictate<"));
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

describe("the Dictate entry never leads nowhere", () => {
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
