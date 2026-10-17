/**
 * Between the editor's document and what the rest of Neo stores about a note.
 *
 * A rich note is its document. When it is saved, three things are derived from
 * it, as Apple Notes derives them:
 *   - the title: the first line that has text;
 *   - the body: Markdown of everything after that line (search, agents, Projects
 *     and Tasks read this; it is a projection and may lose what Markdown cannot
 *     hold, such as colours);
 *   - the tags: every #tag written in the text.
 *
 * A note that predates the editor, or that an agent wrote, arrives as Markdown and
 * is imported here. Nothing is saved by importing. If the import would change
 * what the Markdown says (raw HTML is the usual cause), the result says so, and
 * the caller opens the note read-only until the person chooses to edit it.
 */
import { MarkdownManager } from "@tiptap/markdown";

import { compareMarkdown } from "./fingerprint.js";
import { noteExtensions } from "./noteSchema.js";
import { findTags, normalizeTag } from "./tags.js";

let sharedManager = null;

/** A Markdown parser/serialiser for the note schema, for use without an editor. */
export function markdownManager() {
  if (!sharedManager) {
    sharedManager = new MarkdownManager({
      extensions: noteExtensions(),
      indentation: { style: "space", size: 2 },
    });
  }
  return sharedManager;
}

const TEXTBLOCKS = new Set(["paragraph", "heading", "codeBlock"]);

export function nodeText(node) {
  if (!node) return "";
  if (node.type === "text") return node.text ?? "";
  if (node.type === "hardBreak") return " ";
  // Inline content runs together; blocks (list items, cells, rows) are separate words.
  const separator = TEXTBLOCKS.has(node.type) ? "" : " ";
  return (node.content ?? []).map(nodeText).join(separator);
}

function firstTextLine(node) {
  if (!node) return "";
  const kind = node.type;
  if (kind === "paragraph" || kind === "heading" || kind === "codeBlock") {
    return nodeText(node).split("\n")[0];
  }
  for (const child of node.content ?? []) {
    const line = firstTextLine(child);
    if (line.trim()) return line;
  }
  return "";
}

/** Index of the first top-level block with text, and that text's first line. */
export function firstLine(doc) {
  const blocks = doc?.content ?? [];
  for (let index = 0; index < blocks.length; index += 1) {
    const line = firstTextLine(blocks[index]);
    if (line.trim()) return { index, text: line.replace(/\s+/g, " ").trim() };
  }
  return { index: -1, text: "" };
}

/** The document without its title line, when that line is a whole top-level paragraph or heading. */
export function bodyDoc(doc) {
  const blocks = doc?.content ?? [];
  const { index } = firstLine(doc);
  if (index < 0) return { type: "doc", content: [] };
  const titleBlock = blocks[index];
  const standalone = ["paragraph", "heading"].includes(titleBlock.type)
    && !nodeText(titleBlock).includes("\n");
  return { type: "doc", content: standalone ? blocks.slice(index + 1) : blocks.slice(index) };
}

/** Every #tag written in the document, outside code and links, normalised, in order. */
export function extractTags(doc) {
  const tags = [];
  const seen = new Set();
  function walk(node, inCode) {
    if (!node) return;
    const code = inCode || node.type === "codeBlock";
    if (node.type === "text") {
      if (code || (node.marks ?? []).some((mark) => mark.type === "code" || mark.type === "link")) return;
      for (const { tag } of findTags(node.text)) {
        if (!seen.has(tag)) {
          seen.add(tag);
          tags.push(tag);
        }
      }
      return;
    }
    for (const child of node.content ?? []) walk(child, code);
  }
  walk(doc, false);
  return tags;
}

/** Title, body and tags for saving a rich note. */
export function projectNote(doc, manager = markdownManager()) {
  const { text } = firstLine(doc);
  return {
    title: text.slice(0, 200),
    body: manager.serialize(bodyDoc(doc)).trim(),
    tags: extractTags(doc),
  };
}

/** The two lines a list row shows while someone types: title, then the next line. */
export function liveRow(doc) {
  const blocks = doc?.content ?? [];
  const { index, text } = firstLine(doc);
  let preview = "";
  for (let i = index + 1; i < blocks.length && !preview; i += 1) {
    preview = nodeText(blocks[i]).replace(/\s+/g, " ").trim();
  }
  if (!preview && index >= 0) {
    const rest = nodeText(blocks[index]).replace(/\s+/g, " ").trim();
    preview = rest.startsWith(text) ? rest.slice(text.length).trim() : "";
  }
  return { title: text, preview: preview.slice(0, 160) };
}

const STYLE_BLOCK = {
  title: { type: "heading", attrs: { level: 1 } },
  heading: { type: "heading", attrs: { level: 2 } },
  subheading: { type: "heading", attrs: { level: 3 } },
  body: { type: "paragraph" },
};

/** An empty note that starts in the chosen paragraph style (Apple's "New notes start with"). */
export function newNoteDoc(style = "title") {
  return { type: "doc", content: [{ ...(STYLE_BLOCK[style] ?? STYLE_BLOCK.title) }] };
}

function hashtagLine(tags) {
  return {
    type: "paragraph",
    content: [{ type: "text", text: tags.map((tag) => `#${tag}`).join(" ") }],
  };
}

/**
 * Tags that exist only in the note's tag list (added by research, an agent, or the
 * old tag field) are shown as a closing line of #tags, Apple's way. It is part of
 * the displayed document only; it is saved if, and when, the note is edited.
 */
export function withTagLine(doc, tags) {
  const present = new Set(extractTags(doc));
  const missing = (tags ?? []).map(normalizeTag).filter((tag) => tag && !present.has(tag));
  if (!missing.length) return doc;
  return { ...doc, content: [...(doc.content ?? []), hashtagLine([...new Set(missing)])] };
}

/**
 * A stored note as a document for the editor.
 *
 * Returns `{ doc, lossy, reasons }`. `lossy` is true when importing Markdown would
 * change what it says; the original is untouched either way, since nothing here
 * saves.
 */
export function noteToDoc(note, manager = markdownManager()) {
  if (note?.content_format === "rich" && note.doc) {
    return { doc: withTagLine(note.doc, note.tags), lossy: false, reasons: [] };
  }
  const body = String(note?.body ?? "");
  const parsed = body.trim() ? manager.parse(body) : { type: "doc", content: [] };
  const check = body.trim()
    ? compareMarkdown(body, manager.serialize(parsed))
    : { equal: true, reasons: [] };
  const title = String(note?.title ?? "").replace(/\s+/g, " ").trim();
  let content = parsed.content ?? [];
  if (title && firstLine(parsed).text !== title) {
    content = [{ type: "heading", attrs: { level: 1 }, content: [{ type: "text", text: title }] }, ...content];
  }
  if (!content.length) content = newNoteDoc("title").content;
  const doc = withTagLine({ type: "doc", content }, note?.tags);
  return { doc, lossy: !check.equal, reasons: check.reasons };
}
