/**
 * What a piece of Markdown means, as opposed to how it is spelled.
 *
 * The editor's document is the truth for a rich note, and Markdown is how a note
 * that predates the editor (or that an agent wrote) arrives. Converting one into
 * the other can lose things the editor has no place for: raw HTML is the usual
 * one. This module is how that loss is noticed instead of happening silently.
 *
 * A fingerprint keeps what a reader would get from the Markdown -- block
 * structure, heading levels, the text, code and its language, link and image
 * targets, checklist state, table cells, and any raw HTML verbatim -- and drops
 * what is only spelling: list markers, emphasis delimiters, escapes, blank lines,
 * table padding and alignment, loose versus tight lists. Two Markdown strings
 * with equal fingerprints say the same thing; when an import changes the
 * fingerprint, something the reader would have seen is gone.
 */
import { Lexer } from "marked";

const ENTITIES = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " ", "#39": "'" };

function decode(text) {
  return String(text ?? "")
    .replace(/&(#x[0-9a-f]+|#\d+|[a-z]+\d*|#39);/gi, (match, name) => {
      const lower = name.toLowerCase();
      if (lower.startsWith("#x")) return String.fromCodePoint(Number.parseInt(lower.slice(2), 16));
      if (lower.startsWith("#")) return String.fromCodePoint(Number.parseInt(lower.slice(1), 10));
      return ENTITIES[lower] ?? match;
    })
    .replace(/ /g, " ");
}

function squash(text) {
  return text.replace(/\s+/g, " ").trim();
}

/** Inline tokens flattened to text, with link and image targets and HTML kept visible. */
function inline(tokens) {
  let out = "";
  for (const token of tokens ?? []) {
    switch (token.type) {
      case "link":
        out += `[${inline(token.tokens)}](${token.href})`;
        break;
      case "image":
        out += `![${decode(token.text)}](${token.href})`;
        break;
      case "codespan":
        out += `\`${decode(token.text)}\``;
        break;
      case "html":
        out += `<html:${token.raw.trim()}>`;
        break;
      case "br":
        out += " ";
        break;
      case "checkbox":
        break;
      default:
        if (token.tokens?.length) out += inline(token.tokens);
        else out += decode(token.text ?? token.raw ?? "");
    }
  }
  return out;
}

function blocks(tokens) {
  const out = [];
  for (const token of tokens ?? []) {
    switch (token.type) {
      case "space":
      case "def":
      case "checkbox":
        break;
      case "paragraph":
      case "text": {
        const text = squash(inline(token.tokens ?? [token]));
        if (text) out.push(["p", text]);
        break;
      }
      case "heading":
        out.push(["h", token.depth, squash(inline(token.tokens))]);
        break;
      case "code":
        out.push(["code", (token.lang || "").trim(), token.text.replace(/\s+$/, "")]);
        break;
      case "blockquote":
        out.push(["quote", ...blocks(token.tokens)]);
        break;
      case "list":
        out.push([
          "list",
          token.ordered ? Number(token.start || 1) : "-",
          ...token.items.map((item) => [
            "li",
            item.task ? (item.checked ? "x" : " ") : "",
            ...blocks(item.tokens),
          ]),
        ]);
        break;
      case "table":
        out.push([
          "table",
          token.header.map((cell) => squash(inline(cell.tokens))),
          ...token.rows.map((row) => row.map((cell) => squash(inline(cell.tokens)))),
        ]);
        break;
      case "hr":
        out.push(["hr"]);
        break;
      case "html": {
        const raw = token.raw.trim();
        if (raw) out.push(["html", raw]);
        break;
      }
      default:
        out.push([token.type, squash(token.raw ?? "")]);
    }
  }
  return out;
}

export function markdownFingerprint(markdown) {
  return blocks(new Lexer({ gfm: true }).lex(String(markdown ?? "")));
}

function contains(tree, kind) {
  return Array.isArray(tree)
    && (tree[0] === kind || tree.some((child) => Array.isArray(child) && contains(child, kind)));
}

/**
 * Compare an original with what came back from a round trip.
 * `reasons` names what kind of content was affected, for the person reading the banner.
 */
export function compareMarkdown(original, roundTripped) {
  const before = markdownFingerprint(original);
  const after = markdownFingerprint(roundTripped);
  const equal = JSON.stringify(before) === JSON.stringify(after);
  if (equal) return { equal, reasons: [] };
  const reasons = [];
  const beforeText = JSON.stringify(before);
  if (before.some((block) => contains(block, "html")) || beforeText.includes("<html:")) {
    reasons.push("HTML");
  }
  if (!reasons.length) reasons.push("formatting");
  return { equal, reasons };
}
