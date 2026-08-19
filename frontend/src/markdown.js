const ESCAPE_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ESCAPE_MAP[character]);
}

/** Only linkable schemes survive; anything else stays literal text. */
function safeUrl(raw) {
  const url = raw.trim();
  return /^(https?:\/\/|mailto:)/i.test(url) ? url : null;
}

/** Code spans are split out first so their contents never pick up bold or italic. */
function renderInline(escaped) {
  return escaped
    .split(/(`[^`]+`)/g)
    .map((segment) => {
      if (segment.length > 1 && segment.startsWith("`") && segment.endsWith("`")) {
        return `<code>${segment.slice(1, -1)}</code>`;
      }
      return segment
        .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (match, label, href) => {
          const url = safeUrl(href);
          return url
            ? `<a href="${url}" target="_blank" rel="noreferrer noopener">${label}</a>`
            : match;
        })
        .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
        .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    })
    .join("");
}

function inline(text) {
  return renderInline(escapeHtml(text));
}

// A fence opens with at least three backticks or tildes and may carry an info string.
// CommonMark forbids a backtick inside that info string, which keeps a single-line
// `` `a` `` span from being mistaken for the start of a block.
const FENCE_OPEN = /^(`{3,}|~{3,})[ \t]*([^`]*)$/;
const FENCE_CLOSE = /^(`{3,}|~{3,})[ \t]*$/;

function openingFence(line) {
  const match = FENCE_OPEN.exec(line.trim());
  return match ? { marker: match[1][0], width: match[1].length, info: match[2].trim() } : null;
}

/** A fence only closes on its own marker, at least as wide as the one that opened it. */
function closesFence(line, opening) {
  const match = FENCE_CLOSE.exec(line.trim());
  return Boolean(match) && match[1][0] === opening.marker && match[1].length >= opening.width;
}

/**
 * Turns the info string into a `language-*` class.
 *
 * Only a single plain token qualifies. The whitelist excludes quotes and angle
 * brackets, so the value cannot break out of the attribute, and an unrecognised
 * info string simply yields an unlabelled block rather than raw markup.
 */
function languageClass(info) {
  const token = info.split(/\s+/)[0].toLowerCase();
  return /^[a-z0-9][a-z0-9#+._-]{0,29}$/.test(token) ? ` class="language-${token}"` : "";
}

const LIST_ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;

function parseListItem(line) {
  const match = LIST_ITEM.exec(line);
  if (!match) return null;
  return {
    // Tabs count as four columns so a tab-indented sublist nests like a spaced one.
    indent: match[1].replace(/\t/g, "    ").length,
    ordered: /\d/.test(match[2]),
    content: match[3],
  };
}

/**
 * Builds one list and everything nested inside it, returning where it stopped.
 *
 * Nesting is decided by indentation rather than by CommonMark's stricter
 * content-column rule, because models commonly indent a sublist by a single space
 * and the intent is unambiguous.
 */
function buildList(entries, start) {
  const baseIndent = entries[start].indent;
  const ordered = entries[start].ordered;
  const items = [];
  let index = start;

  while (index < entries.length) {
    const entry = entries[index];
    if (entry.indent < baseIndent) break;
    if (entry.indent > baseIndent) {
      const [nested, next] = buildList(entries, index);
      if (items.length) {
        items[items.length - 1] += nested;
      } else {
        items.push(nested);
      }
      index = next;
      continue;
    }
    // A different marker type at the same depth starts a separate list.
    if (entry.ordered !== ordered) break;
    items.push(inline(entry.content));
    index += 1;
  }

  const tag = ordered ? "ol" : "ul";
  return [`<${tag}>${items.map((item) => `<li>${item}</li>`).join("")}</${tag}>`, index];
}

/** Splits a table row on unescaped pipes, dropping the optional leading/trailing one. */
function splitRow(line) {
  let text = line.trim();
  if (text.startsWith("|")) text = text.slice(1);
  if (text.endsWith("|") && !text.endsWith("\\|")) text = text.slice(0, -1);
  return text.split("|").map((cell) => cell.trim());
}

/**
 * Reads a table's delimiter row into per-column alignments.
 *
 * Returns null when the line is not a delimiter row, which is what distinguishes a
 * real table from an ordinary paragraph that merely contains a pipe.
 */
function delimiterAlignments(line) {
  if (typeof line !== "string" || !line.includes("-") || !line.includes("|")) return null;
  const cells = splitRow(line);
  if (!cells.length) return null;
  const alignments = [];
  for (const cell of cells) {
    const match = /^(:?)-+(:?)$/.exec(cell.replace(/\s+/g, ""));
    if (!match) return null;
    const [, left, right] = match;
    alignments.push(left && right ? "center" : right ? "right" : left ? "left" : "");
  }
  return alignments;
}

/** Alignment comes from a fixed set, so it is safe to inline as a style. */
function alignStyle(alignment) {
  return alignment ? ` style="text-align:${alignment}"` : "";
}

function renderTable(headerLine, alignments, bodyLines) {
  const columns = alignments.length;
  const cell = (tag, text, index) =>
    `<${tag}${alignStyle(alignments[index] || "")}>${inline(text ?? "")}</${tag}>`;

  const header = splitRow(headerLine)
    .slice(0, columns)
    .map((text, index) => cell("th", text, index))
    .join("");

  const rows = bodyLines
    .map((line) => {
      const values = splitRow(line);
      // Short rows are padded and long ones clipped, so a ragged row cannot shift
      // the column count and break the table layout.
      const cells = Array.from({ length: columns }, (_, index) =>
        cell("td", values[index] ?? "", index),
      );
      return `<tr>${cells.join("")}</tr>`;
    })
    .join("");

  return `<table><thead><tr>${header}</tr></thead><tbody>${rows}</tbody></table>`;
}

function isTableStart(lines, index) {
  const line = lines[index];
  return (
    typeof line === "string" &&
    line.includes("|") &&
    delimiterAlignments(lines[index + 1]) !== null
  );
}

function startsNewBlock(lines, index) {
  const line = lines[index];
  return (
    !line.trim() ||
    openingFence(line) !== null ||
    /^#{1,6}\s/.test(line) ||
    /^\s*>/.test(line) ||
    parseListItem(line) !== null ||
    isTableStart(lines, index)
  );
}

/**
 * Deliberately small Markdown subset shared by notes and chat. Everything is
 * HTML-escaped before a single tag is introduced, so untrusted model output and
 * pasted user text are both safe to render.
 */
export function renderMarkdown(source) {
  const lines = String(source ?? "").split(/\r?\n/);
  const html = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];
    const opening = openingFence(line);

    if (opening) {
      const buffer = [];
      index += 1;
      // An unterminated fence runs to the end of the source. That is what a code
      // block looks like mid-stream, so a partial answer renders the same way the
      // finished one will.
      while (index < lines.length && !closesFence(lines[index], opening)) {
        buffer.push(lines[index]);
        index += 1;
      }
      index += 1;
      const code = escapeHtml(buffer.join("\n"));
      html.push(`<pre><code${languageClass(opening.info)}>${code}</code></pre>`);
      continue;
    }

    if (!line.trim()) {
      index += 1;
      continue;
    }

    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      html.push("<hr />");
      index += 1;
      continue;
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      const level = heading[1].length;
      html.push(`<h${level}>${inline(heading[2].trim())}</h${level}>`);
      index += 1;
      continue;
    }

    if (isTableStart(lines, index)) {
      const alignments = delimiterAlignments(lines[index + 1]);
      const headerLine = line;
      index += 2;
      const body = [];
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
        body.push(lines[index]);
        index += 1;
      }
      html.push(renderTable(headerLine, alignments, body));
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      const buffer = [];
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
        buffer.push(lines[index].replace(/^\s*>\s?/, ""));
        index += 1;
      }
      html.push(`<blockquote>${inline(buffer.join(" "))}</blockquote>`);
      continue;
    }

    if (parseListItem(line)) {
      const entries = [];
      while (index < lines.length) {
        const entry = parseListItem(lines[index]);
        if (!entry) break;
        entries.push(entry);
        index += 1;
      }
      let cursor = 0;
      while (cursor < entries.length) {
        const [listHtml, next] = buildList(entries, cursor);
        html.push(listHtml);
        cursor = next;
      }
      continue;
    }

    const buffer = [];
    while (index < lines.length && !startsNewBlock(lines, index)) {
      buffer.push(lines[index]);
      index += 1;
    }
    html.push(`<p>${inline(buffer.join("\n"))}</p>`);
  }

  return html.join("");
}
