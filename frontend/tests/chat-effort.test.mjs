/**
 * The effort tooltip mentions a model that always reasons only after a Low
 * reply from that model showed it. Nothing here is inferred from a model name.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { ChatComposer } from "../src/App.jsx";
import { ALWAYS_REASONS_NOTE, effortTooltip, modelAlwaysReasons } from "../src/chatEffort.js";

const reply = (effort, flag, model = "m:latest") => ({
  role: "assistant",
  model_name: model,
  metadata: { timings: { effort, ...(flag ? { model_always_reasons: true } : {}) } },
});

test("a Low reply that reasoned anyway turns the note on", () => {
  assert.equal(modelAlwaysReasons([reply("low", true)], "m:latest"), true);
  assert.ok(effortTooltip(true).includes(ALWAYS_REASONS_NOTE));
});

test("the latest Low reply from the chosen model decides", () => {
  const messages = [reply("low", true), reply("low", false)];
  assert.equal(modelAlwaysReasons(messages, "m:latest"), false);
  assert.equal(modelAlwaysReasons([reply("low", false), reply("low", true)], ""), true);
});

test("High replies and other models say nothing about Low", () => {
  assert.equal(modelAlwaysReasons([reply("high", true)], "m:latest"), false);
  assert.equal(modelAlwaysReasons([reply("low", true, "other:7b")], "m:latest"), false);
  assert.equal(modelAlwaysReasons([{ role: "user", metadata: {} }], ""), false);
  assert.equal(modelAlwaysReasons(undefined), false);
});

test("without the flag the tooltip is the plain Low and High description", () => {
  const text = effortTooltip(false);
  assert.ok(text.startsWith("Quick: shorter answers"));
  assert.ok(text.includes("Detailed: fuller answers"));
  assert.ok(!text.includes(ALWAYS_REASONS_NOTE));
});

test("Quick and Detailed are visible beside Chat, not hidden in the composer menu", () => {
  const html = renderToStaticMarkup(createElement(ChatComposer, {
    disabled: false,
    value: "",
    onChange() {},
    onSubmit() {},
    llms: [{ id: "local", name: "Local", model: "model", enabled: true }],
    llmId: "local",
    onLlmChange() {},
    mode: "chatbot",
    onModeChange() {},
    effort: "low",
    onEffortChange() {},
    repos: [],
    agentDefinitions: [],
  }));
  const footer = html.slice(html.indexOf('class="composer-foot"'));
  assert.ok(footer.includes('class="agent-chip chat-answer-style"'));
  assert.ok(footer.includes('aria-label="Choose answer style"'));
  assert.ok(footer.includes('>Quick</option>'));
  assert.ok(footer.includes('>Detailed</option>'));
});
