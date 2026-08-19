import assert from "node:assert/strict";
import test from "node:test";

import {
  formatDuration,
  formatResponseKind,
  formatTokens,
  renderMessageHtml,
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

test("a fenced block renders as a code block instead of literal backticks", () => {
  const html = renderMessageHtml("Before\n\n```\nplain code\n```\n\nAfter");

  assert.match(html, /<pre><code>plain code<\/code><\/pre>/);
  assert.ok(!html.includes("```"), "no raw fence artifacts survive");
  assert.match(html, /<p>Before<\/p>/);
  assert.match(html, /<p>After<\/p>/);
});

test("a language tag becomes a language class and never escapes the attribute", () => {
  assert.match(
    renderMessageHtml('```python\ndef greet(name):\n    return f"Hello, {name}!"\n```'),
    /<pre><code class="language-python">/,
  );
  assert.match(renderMessageHtml("```  JavaScript  \nlet a = 1;\n```"), /class="language-javascript"/);
  assert.match(renderMessageHtml("```c++\nint main() {}\n```"), /class="language-c\+\+/);
  const hostile = renderMessageHtml('```"><img src=x onerror=alert(1)>\ncode\n```');
  assert.match(hostile, /<pre><code>/);
  assert.ok(!hostile.includes("<img"), "an unsafe info string never reaches the DOM");
});

test("code inside a fence is escaped and keeps its own markup literal", () => {
  const html = renderMessageHtml("```html\n<script>alert('x')</script>\n```");

  assert.ok(!html.includes("<script>"), "script tags never survive as markup");
  assert.match(html, /&lt;script&gt;alert\(&#39;x&#39;\)&lt;\/script&gt;/);
});

test("a fence body keeps markdown, indentation and blank lines verbatim", () => {
  const html = renderMessageHtml("```python\ndef f():\n\n    return '**not bold**'\n```");

  assert.match(html, /def f\(\):\n\n    return &#39;\*\*not bold\*\*&#39;/);
  assert.ok(!html.includes("<strong>"), "emphasis inside a fence stays literal");
});

test("an unterminated fence still renders as a code block while streaming", () => {
  const html = renderMessageHtml("```python\ndef greet(name):");

  assert.match(html, /<pre><code class="language-python">def greet\(name\):<\/code><\/pre>/);
});

test("tilde fences work and are not closed by a backtick fence", () => {
  assert.match(renderMessageHtml("~~~python\nvalue = 1\n~~~"), /class="language-python">value = 1</);
  assert.match(renderMessageHtml("~~~\n```\nstill inside\n~~~"), /<pre><code>```\nstill inside</);
});

test("a pipe table renders as a real table with header and body rows", () => {
  const html = renderMessageHtml("| Name | Age |\n| --- | --- |\n| Soham Chawla | 21 |\n| Ada | 36 |");

  assert.match(html, /<table><thead><tr><th>Name<\/th><th>Age<\/th><\/tr><\/thead>/);
  assert.match(html, /<tbody><tr><td>Soham Chawla<\/td><td>21<\/td><\/tr>/);
  assert.match(html, /<tr><td>Ada<\/td><td>36<\/td><\/tr><\/tbody><\/table>/);
  assert.ok(!html.includes("| Name |"), "no raw pipe syntax survives");
});

test("table alignment markers become text-align and cells keep inline markdown", () => {
  const html = renderMessageHtml("| L | C | R |\n| :--- | :---: | ---: |\n| **a** | b | `c` |");

  assert.match(html, /<th style="text-align:left">L<\/th>/);
  assert.match(html, /<th style="text-align:center">C<\/th>/);
  assert.match(html, /<th style="text-align:right">R<\/th>/);
  assert.match(html, /<td style="text-align:left"><strong>a<\/strong><\/td>/);
  assert.match(html, /<td style="text-align:right"><code>c<\/code><\/td>/);
});

test("a ragged table row is padded rather than breaking the column count", () => {
  const html = renderMessageHtml("| A | B | C |\n| - | - | - |\n| only one |\n| 1 | 2 | 3 | 4 |");

  assert.match(html, /<tr><td>only one<\/td><td><\/td><td><\/td><\/tr>/);
  assert.match(html, /<tr><td>1<\/td><td>2<\/td><td>3<\/td><\/tr>/);
});

test("a paragraph containing a pipe is not mistaken for a table", () => {
  const html = renderMessageHtml("Use a | b to pipe output.");

  assert.match(html, /<p>Use a \| b to pipe output\.<\/p>/);
  assert.ok(!html.includes("<table"), "a lone pipe never starts a table");
});

test("nested bullet lists nest instead of flattening", () => {
  const html = renderMessageHtml(
    [
      "* Level One Topic A",
      " * Sub-point 1: Detail about A",
      " * Sub-point 2: Another detail for A",
      "* Level One Topic B",
      " * Sub-point 1: Information regarding B",
      "  * Deep dive point i",
    ].join("\n"),
  );

  assert.match(html, /<li>Level One Topic A<ul><li>Sub-point 1: Detail about A<\/li>/);
  assert.match(html, /<li>Sub-point 1: Information regarding B<ul><li>Deep dive point i<\/li><\/ul><\/li>/);
  // One outer list holding both top-level topics, not four sibling lists.
  assert.equal(html.match(/<ul>/g).length, html.match(/<\/ul>/g).length);
  assert.ok(html.startsWith("<ul><li>Level One Topic A"), "a single outer list wraps everything");
});

test("ordered lists nest and stay distinct from bullet lists", () => {
  const html = renderMessageHtml("1. First\n   1. Inner\n2. Second");

  assert.match(html, /<ol><li>First<ol><li>Inner<\/li><\/ol><\/li><li>Second<\/li><\/ol>/);
  assert.ok(!html.includes("<ul>"), "an ordered list never becomes a bullet list");
});

test("unicode, emoji and RTL text survive rendering unchanged", () => {
  const source = [
    "English: Hello Neo 👋🚀",
    "Hindi: नमस्ते दुनिया",
    "Arabic: مرحبا بالعالم",
    "Hebrew: שלום עולם",
    "Japanese: こんにちは世界",
  ].join("\n");

  const html = renderMessageHtml(source);

  for (const line of source.split("\n")) {
    assert.ok(html.includes(line), `${line} survives rendering`);
  }
});

test("the CHT-12 message renders both its unicode lines and its code block", () => {
  const html = renderMessageHtml(
    [
      "English: Hello Neo 👋🚀",
      "Hindi: नमस्ते दुनिया",
      "Arabic: مرحبا بالعالم",
      "",
      "```python",
      "def greet(name):",
      '    return f"Hello, {name}! 👋"',
      "```",
    ].join("\n"),
  );

  assert.match(html, /<p>English: Hello Neo 👋🚀\nHindi: नमस्ते दुनिया\nArabic: مرحبا بالعالم<\/p>/);
  assert.match(html, /<pre><code class="language-python">def greet\(name\):/);
  assert.match(html, /return f&quot;Hello, \{name\}! 👋&quot;/);
  assert.ok(!html.includes("```"), "no raw fence artifacts survive");
});

test("inline markdown and unsafe links keep their existing protections", () => {
  assert.match(renderMessageHtml("**bold** and *italic*"), /<strong>bold<\/strong>/);
  assert.match(renderMessageHtml("use `inline code` here"), /<code>inline code<\/code>/);
  assert.match(
    renderMessageHtml("[link](https://example.com)"),
    /<a href="https:\/\/example.com" target="_blank" rel="noreferrer noopener">link<\/a>/,
  );
  const unsafe = renderMessageHtml("[click](javascript:alert(1))");
  assert.ok(!unsafe.includes("<a "), "javascript: never becomes a link");
});

test("thematic breaks render without swallowing bold text", () => {
  const html = renderMessageHtml("***\n\n**Nested Bullet List:**\n\n***");

  assert.equal(html.match(/<hr \/>/g).length, 2);
  assert.match(html, /<p><strong>Nested Bullet List:<\/strong><\/p>/);
});

test("an assistant markdown https link becomes a real anchor with its href intact", () => {
  // The exact string CHT-13 asks the model to emit, as stored by the backend.
  const stored = "[Neo source repository](https://github.com/chwla/neo)";
  const html = renderMessageHtml(stored);

  assert.match(
    html,
    /<a href="https:\/\/github\.com\/chwla\/neo" target="_blank" rel="noreferrer noopener">Neo source repository<\/a>/,
  );
  assert.ok(!html.includes("]("), "no half-eaten link syntax survives");
});

test("a truncated link left by url stripping never renders as an anchor", () => {
  // What the old backend produced. It must read as plain text, not broken markup.
  const html = renderMessageHtml("[Neo source repository](");

  assert.ok(!html.includes("<a "), "an incomplete link is not linkified");
  assert.match(html, /<p>\[Neo source repository\]\(<\/p>/);
});

test("unsafe url schemes stay blocked while http and https survive", () => {
  for (const scheme of [
    "javascript:alert(1)",
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    "vbscript:msgbox(1)",
    "file:///etc/passwd",
  ]) {
    const html = renderMessageHtml(`[click](${scheme})`);
    assert.ok(!html.includes("<a "), `${scheme} never becomes a link`);
    assert.ok(!html.includes("href"), `${scheme} never reaches an href`);
  }

  assert.match(renderMessageHtml("[plain](http://example.com)"), /href="http:\/\/example\.com"/);
  assert.match(renderMessageHtml("[secure](https://example.com)"), /href="https:\/\/example\.com"/);
  assert.match(renderMessageHtml("[mail](mailto:a@b.test)"), /href="mailto:a@b\.test"/);
});

test("a link label cannot smuggle markup through the anchor", () => {
  const html = renderMessageHtml('[<img src=x onerror=alert(1)>](https://example.com)');

  assert.ok(!html.includes("<img"), "label markup is escaped");
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
});

test("raw html in a message body is escaped rather than rendered", () => {
  const html = renderMessageHtml('<img src=x onerror="alert(1)"> plain text');

  assert.ok(!html.includes("<img"), "injected markup never reaches the DOM");
  assert.match(html, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;/);
});

test("a script tag inside an inline code span stays inert text", () => {
  const html = renderMessageHtml("`<script>alert('xss')</script>`");

  assert.ok(!html.includes("<script>"), "the script tag never reaches the DOM");
  assert.match(html, /<code>&lt;script&gt;alert\(&#39;xss&#39;\)&lt;\/script&gt;<\/code>/);
});
