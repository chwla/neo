import assert from "node:assert/strict";
import test from "node:test";

import {
  formatDuration,
  formatResponseKind,
  formatTokens,
  splitGeneratedText,
} from "../src/chatPresentation.js";

test("direct responses show meaningful response-kind metadata without n/a", () => {
  const message = {
    response_kind: "direct_memory",
    total_tokens: null,
    duration_ms: 18,
  };

  assert.equal(formatResponseKind(message), "Memory");
  assert.equal(formatTokens(message), null);
  assert.equal(formatDuration(message.duration_ms), "18 ms");
});

test("model responses show provider and model with token and duration metadata", () => {
  const message = {
    provider_name: "Ollama",
    model_name: "qwen",
    total_tokens: 321,
    duration_ms: 2345,
  };

  assert.equal(formatResponseKind(message), "Ollama / qwen");
  assert.equal(formatTokens(message), "321 tokens");
  assert.equal(formatDuration(message.duration_ms), "2.3 s");
});

test("thinking blocks are separated from visible answer without duplicate text", () => {
  const parsed = splitGeneratedText(
    "<think>first reason</think>Hello <think>second reason</think>world",
  );

  assert.equal(parsed.content, "Hello world");
  assert.equal(parsed.thinking, "first reason\n\nsecond reason");
});

test("an incomplete streamed thinking block never leaks into the answer", () => {
  const parsed = splitGeneratedText("Visible answer.<think>still reasoning");

  assert.equal(parsed.content, "Visible answer.");
  assert.equal(parsed.thinking, "still reasoning");
});
