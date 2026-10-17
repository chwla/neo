/**
 * Markdown in, document out, and back: the import guard and the save projection.
 *
 * These run in Node, where raw HTML inside Markdown stays literal text. The
 * browser parses it through the schema instead, which is why the same fixtures
 * also run in real browsers (bench/notes-fidelity.html); this file pins the
 * rules that do not depend on a DOM.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { compareMarkdown, markdownFingerprint } from "../src/notes/fingerprint.js";
import {
  bodyDoc,
  extractTags,
  firstLine,
  liveRow,
  markdownManager,
  newNoteDoc,
  noteToDoc,
  projectNote,
} from "../src/notes/projection.js";
import { getSchema, resolveExtensions } from "@tiptap/core";

import { noteExtensions } from "../src/notes/noteSchema.js";
import { findTags, normalizeTag } from "../src/notes/tags.js";
import { MARKDOWN } from "../bench/notes-fixtures.js";

const text = (value) => ({ type: "text", text: value });
const p = (...content) => ({ type: "paragraph", content });
const h = (level, value) => ({ type: "heading", attrs: { level }, content: [text(value)] });

const FIXTURES = MARKDOWN;

describe("fingerprint", () => {
  test("spelling differences do not change meaning", () => {
    assert.equal(compareMarkdown("* a\n* b\n", "- a\n- b\n").equal, true);
    assert.equal(compareMarkdown("*a* __b__", "_a_ **b**").equal, true);
    assert.equal(compareMarkdown("| a | b |\n|---|---|\n| 1 | 2 |", "| a   | b |\n| - | - |\n| 1 | 2 |").equal, true);
    assert.equal(compareMarkdown("a\n\n\n\nb", "a\n\nb").equal, true);
    assert.equal(compareMarkdown("- a\n\n- b\n", "- a\n- b\n").equal, true);
    assert.equal(compareMarkdown("[x][r]\n\n[r]: https://e.com", "[x](https://e.com)").equal, true);
  });

  test("changes a reader would notice are caught", () => {
    assert.equal(compareMarkdown("hello", "hallo").equal, false);
    assert.equal(compareMarkdown("[a](https://one)", "[a](https://two)").equal, false);
    assert.equal(compareMarkdown("- [ ] x", "- [x] x").equal, false);
    assert.equal(compareMarkdown("# a", "## a").equal, false);
    assert.equal(compareMarkdown("```js\nx\n```", "```\nx\n```").equal, false);
    assert.equal(compareMarkdown("3. a", "1. a").equal, false);
  });

  test("lost HTML is named as the reason", () => {
    const result = compareMarkdown("<details>x</details>\n\ntext", "&lt;details&gt;x&lt;/details&gt;\n\ntext");
    assert.equal(result.equal, false);
    assert.deepEqual(result.reasons, ["HTML"]);
  });

  test("a fingerprint keeps raw HTML verbatim", () => {
    assert.deepEqual(markdownFingerprint("<!-- note -->"), [["html", "<!-- note -->"]]);
  });
});

describe("tags", () => {
  test("what counts as a tag", () => {
    const found = (value) => findTags(value).map((item) => item.tag);
    assert.deepEqual(found("#work and #Home-Ideas"), ["work", "home-ideas"]);
    assert.deepEqual(found("C# and page#anchor and https://x.dev/#hash"), []);
    assert.deepEqual(found("#1 is not, #v2 is"), ["v2"]);
    assert.deepEqual(found("(#café) [#日本]"), ["café", "日本"]);
    assert.equal(normalizeTag("  #Some Tag "), "some-tag");
  });

  test("tags in code and links are not tags", () => {
    const doc = {
      type: "doc",
      content: [
        p(text("#real "), { type: "text", text: "#incode", marks: [{ type: "code" }] }),
        p({ type: "text", text: "#linked", marks: [{ type: "link", attrs: { href: "https://x" } }] }),
        { type: "codeBlock", content: [text("#block")] },
        p(text("#real again #second")),
      ],
    };
    assert.deepEqual(extractTags(doc), ["real", "second"]);
  });
});

describe("projection", () => {
  test("the first line with text is the title, and the body is what follows", () => {
    const doc = { type: "doc", content: [p(), h(1, "Groceries"), p(text("milk #home"))] };
    assert.deepEqual(firstLine(doc), { index: 1, text: "Groceries" });
    const projected = projectNote(doc);
    assert.equal(projected.title, "Groceries");
    assert.equal(projected.body, "milk #home");
    assert.deepEqual(projected.tags, ["home"]);
  });

  test("a note that starts with a list keeps the list in its body", () => {
    const doc = {
      type: "doc",
      content: [{ type: "taskList", content: [
        { type: "taskItem", attrs: { checked: false }, content: [p(text("milk"))] },
        { type: "taskItem", attrs: { checked: true }, content: [p(text("eggs"))] },
      ] }],
    };
    assert.equal(projectNote(doc).title, "milk");
    assert.equal(bodyDoc(doc).content.length, 1);
    assert.match(projectNote(doc).body, /- \[ \] milk\n- \[x\] eggs/);
  });

  test("an empty note has an empty title, which the server stores as New Note", () => {
    assert.equal(projectNote(newNoteDoc("title")).title, "");
    assert.deepEqual(newNoteDoc("body").content[0].type, "paragraph");
  });

  test("the live row is the title and the next line", () => {
    const doc = { type: "doc", content: [h(1, "Trip"), p(), p(text("passport, tickets"))] };
    assert.deepEqual(liveRow(doc), { title: "Trip", preview: "passport, tickets" });
  });
});

describe("importing stored notes", () => {
  test("the title is not repeated when the body already starts with it", () => {
    const { doc } = noteToDoc({ title: "Plan", body: "# Plan\n\nsteps", tags: [] });
    assert.equal(doc.content.filter((block) => block.type === "heading").length, 1);
  });

  test("a separate title becomes the first line", () => {
    const { doc, lossy } = noteToDoc({ title: "What is best?", body: "# Findings\n\ntext", tags: [] });
    assert.equal(lossy, false);
    assert.equal(firstLine(doc).text, "What is best?");
    assert.equal(doc.content[1].type, "heading");
  });

  test("tags only in the tag list are shown as a closing line of #tags", () => {
    const { doc } = noteToDoc({ title: "T", body: "has #work already", tags: ["work", "research"] });
    const last = doc.content.at(-1);
    assert.equal(last.content[0].text, "#research");
  });

  test("a title-only note opens as a title line", () => {
    const { doc } = noteToDoc({ title: "Call the bank", body: "", tags: [] });
    assert.equal(firstLine(doc).text, "Call the bank");
  });

  test("rich notes open as stored", () => {
    const stored = { type: "doc", content: [h(1, "Stored")] };
    const { doc, lossy } = noteToDoc({ content_format: "rich", doc: stored, tags: [] });
    assert.deepEqual(doc, stored);
    assert.equal(lossy, false);
  });

  for (const [name, markdown] of Object.entries(FIXTURES)) {
    test(`round trip keeps meaning: ${name}`, () => {
      const note = { title: "Fixture title", body: markdown, tags: [] };
      const { doc, lossy } = noteToDoc(note);
      assert.equal(lossy, false, `${name} was flagged as lossy`);
      const saved = projectNote(doc);
      assert.equal(saved.title, "Fixture title");
      const result = compareMarkdown(markdown, saved.body);
      assert.equal(result.equal, true, `${name} changed:\n${saved.body}`);
    });
  }

  test("HTML in Markdown is flagged, never silently dropped (Node keeps it as text)", () => {
    const { lossy, reasons } = noteToDoc({
      title: "Sources",
      body: "<details><summary>Sources</summary>list</details>\n\ntext",
      tags: [],
    });
    assert.equal(lossy, true);
    assert.deepEqual(reasons, ["HTML"]);
  });

  test("every block and mark type in the schema can be written as Markdown", () => {
    const manager = markdownManager();
    const doc = {
      type: "doc",
      content: [
        h(2, "h"),
        p(text("p "), { type: "text", text: "b", marks: [{ type: "bold" }] },
          { type: "text", text: "u", marks: [{ type: "underline" }] },
          { type: "text", text: "c", marks: [{ type: "textStyle", attrs: { color: "var(--nw-ink-blue)" } }] }),
        { type: "bulletList", attrs: { variant: "dash" }, content: [{ type: "listItem", content: [p(text("dash"))] }] },
        { type: "orderedList", attrs: { start: 1 }, content: [{ type: "listItem", content: [p(text("num"))] }] },
        { type: "taskList", content: [{ type: "taskItem", attrs: { checked: true }, content: [p(text("task"))] }] },
        { type: "codeBlock", attrs: { language: null }, content: [text("mono")] },
        { type: "blockquote", content: [p(text("quote"))] },
        { type: "horizontalRule" },
        { type: "table", content: [{ type: "tableRow", content: [
          { type: "tableCell", content: [p(text("cell"))] },
        ] }] },
      ],
    };
    const out = manager.serialize(doc);
    for (const expected of ["h", "**b**", "u", "c", "dash", "num", "task", "mono", "quote", "cell", "---"]) {
      assert.ok(out.includes(expected), `missing ${expected} in:\n${out}`);
    }
  });
});

describe("attachments in the Markdown mirror", () => {
  const ID = "0f3c2a10-1111-4222-8333-944455556666";
  const FILE = "1a2b3c4d-5555-4666-8777-988899990000";
  const doc = {
    type: "doc",
    content: [
      { type: "heading", attrs: { level: 1 }, content: [{ type: "text", text: "Trip" }] },
      { type: "image", attrs: { src: `/api/files/${ID}/download`, fileId: ID, alt: "beach.jpg" } },
      { type: "fileAttachment", attrs: { fileId: FILE, name: "tickets [final].pdf", size: 2048, mime: "application/pdf" } },
    ],
  };

  test("a picture and a file become links to their downloads", () => {
    const { body } = projectNote(doc, markdownManager());
    assert.ok(body.includes(`![beach.jpg](/api/files/${ID}/download)`), body);
    assert.ok(body.includes(`[tickets final.pdf](/api/files/${FILE}/download)`), body);
  });

  test("a picture read back from Markdown keeps its address", () => {
    const { doc: parsed } = noteToDoc({ title: "Trip", body: `![beach.jpg](/api/files/${ID}/download)`, tags: [] }, markdownManager());
    const image = parsed.content.find((node) => node.type === "image");
    assert.equal(image?.attrs.src, `/api/files/${ID}/download`);
  });

  test("every node and mark in the schema writes Markdown, so none silently drops out of the mirror", () => {
    const manager = markdownManager();
    const schema = getSchema(noteExtensions());
    // Written by something else, or knowingly dropped from the mirror with the text kept.
    const accounted = {
      doc: "the document itself",
      text: "plain text",
      tableRow: "written by the table",
      tableCell: "written by the table",
      tableHeader: "written by the table",
      textStyle: "text colour: the text is kept, the colour is not (a documented loss)",
    };
    const missing = [...Object.keys(schema.nodes), ...Object.keys(schema.marks)].filter(
      (name) => !(name in accounted) && !manager.getHandlersForNodeType(name).length,
    );
    assert.deepEqual(missing, []);
    // And a coloured word still reaches the mirror.
    const coloured = { type: "doc", content: [p({ type: "text", text: "red word", marks: [{ type: "textStyle", attrs: { color: "var(--nw-ink-pink)" } }] })] };
    assert.ok(manager.serialize(coloured).includes("red word"));
  });
});

describe("links between notes in the Markdown mirror", () => {
  const ID = "7d1e5c22-aaaa-4bbb-8ccc-0ddd11112222";
  test("a link is [Title](note://id)", () => {
    const doc = {
      type: "doc",
      content: [
        { type: "heading", attrs: { level: 1 }, content: [{ type: "text", text: "Plan" }] },
        { type: "paragraph", content: [{ type: "text", text: "See " }, { type: "noteLink", attrs: { id: ID, label: "Budget [Q3]", mentionSuggestionChar: ">>" } }] },
      ],
    };
    const { body } = projectNote(doc, markdownManager());
    assert.ok(body.includes(`[Budget Q3](note://${ID})`), body);
  });

  test("read back from Markdown, it is a link that still points at the note", () => {
    const { doc, lossy } = noteToDoc({ title: "Plan", body: `See [Budget](note://${ID})`, tags: [] }, markdownManager());
    assert.equal(lossy, false);
    const marks = JSON.stringify(doc);
    assert.ok(marks.includes(`note://${ID}`), marks);
  });

  test("a click on one is the note's to open, ahead of the editor's link opening", () => {
    // Click handlers run in plugin order; the Link extension's would hand note:// to a new browser tab.
    const names = resolveExtensions(noteExtensions()).map((extension) => extension.name);
    assert.ok(names.indexOf("noteUrlLinks") >= 0 && names.indexOf("noteUrlLinks") < names.indexOf("link"), names.join(" "));
  });
});
