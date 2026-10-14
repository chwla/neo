/**
 * Rendering saved messages once per distinct text.
 *
 * A chat switch, a turn ending or a compaction replaces every message object,
 * and each row used to re-run the Markdown renderer. The cache must be exactly
 * the renderer (same output for every input), must stay bounded, must never
 * hold text that is still streaming, and must be dropped on a profile change.
 */
import assert from "node:assert/strict";
import { beforeEach, describe, test } from "node:test";

import {
  clearMessageHtmlCache,
  formatMessageTime,
  messageHtmlCacheStats,
  parseNeoTimestamp,
  renderMessageHtml,
  renderStreamingHtml,
} from "../src/chatPresentation.js";
import { renderMarkdown } from "../src/markdown.js";

const CORPUS = [
  "",
  null,
  undefined,
  "Plain text",
  "# Heading\n\nParagraph with **bold**, *em* and `code`.",
  "- one\n- two\n  - nested\n1. first\n2. second",
  "```js\nconst x = 1 < 2;\n```",
  "```\nunterminated fence",
  "| a | b |\n| --- | ---: |\n| 1 | 2 |",
  "> quoted <script>alert(1)</script>",
  "[link](https://example.com) and [bad](javascript:alert(1))",
  "Ünïcödé, emoji 😀 and CJK 漢字",
  "---",
];

beforeEach(() => clearMessageHtmlCache());

describe("the saved-message cache", () => {
  test("is exactly the renderer, for every kind of input", () => {
    for (const input of CORPUS) {
      assert.equal(renderMessageHtml(input), renderMarkdown(input), JSON.stringify(input));
      assert.equal(renderMessageHtml(input), renderMarkdown(input), `second call: ${JSON.stringify(input)}`);
    }
  });

  test("serves a repeat from the cache", () => {
    renderMessageHtml("Same text");
    const after = messageHtmlCacheStats();
    renderMessageHtml("Same text");
    assert.deepEqual(messageHtmlCacheStats(), after);
    assert.equal(after.entries, 1);
  });

  test("stays within its entry bound", () => {
    for (let index = 0; index < 520; index += 1) renderMessageHtml(`message ${index}`);
    assert.equal(messageHtmlCacheStats().entries, 500);
  });

  test("stays within its character bound", () => {
    const body = "x".repeat(60_000);
    for (let index = 0; index < 80; index += 1) renderMessageHtml(`${index} ${body}`);
    const { entries, chars } = messageHtmlCacheStats();
    assert.ok(chars <= 4_000_000, `${chars} characters held`);
    assert.ok(entries < 80, "the oldest were evicted to make room");
  });

  test("does not keep very long bodies", () => {
    const html = renderMessageHtml("y".repeat(150_000));
    assert.equal(html, renderMarkdown("y".repeat(150_000)));
    assert.equal(messageHtmlCacheStats().entries, 0);
  });

  test("never holds text that is still streaming", () => {
    renderStreamingHtml("partial repl");
    renderStreamingHtml("partial reply");
    assert.equal(messageHtmlCacheStats().entries, 0);
    assert.equal(renderStreamingHtml("**a**"), renderMarkdown("**a**"));
  });

  test("can be emptied, as a profile change does", () => {
    renderMessageHtml("one");
    renderMessageHtml("two");
    clearMessageHtmlCache();
    assert.deepEqual(messageHtmlCacheStats(), { entries: 0, chars: 0 });
  });
});

describe("message times", () => {
  test("read exactly as toLocaleTimeString did", () => {
    for (const value of ["2026-10-14T09:05:00Z", "2026-10-14T21:45:30+05:30", "2026-10-14T00:00:00"]) {
      const expected = new Date(parseNeoTimestamp(value)).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      assert.equal(formatMessageTime(value), expected, value);
    }
    assert.equal(formatMessageTime("not a time"), null);
    assert.equal(formatMessageTime(""), null);
  });
});
