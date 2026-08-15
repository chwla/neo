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

function startsNewBlock(line) {
  return (
    !line.trim() ||
    /^```/.test(line.trim()) ||
    /^#{1,6}\s/.test(line) ||
    /^\s*>/.test(line) ||
    /^\s*([-*+]|\d+[.)])\s+/.test(line)
  );
}

/**
 * Deliberately small Markdown subset: the note body is written by one local user,
 * and everything is HTML-escaped before a single tag is introduced.
 */
export function renderMarkdown(source) {
  const lines = String(source ?? "").split(/\r?\n/);
  const html = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];

    if (/^```/.test(line.trim())) {
      const buffer = [];
      index += 1;
      while (index < lines.length && !/^```/.test(lines[index].trim())) {
        buffer.push(lines[index]);
        index += 1;
      }
      index += 1;
      html.push(`<pre><code>${escapeHtml(buffer.join("\n"))}</code></pre>`);
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
      html.push(`<h${level}>${renderInline(escapeHtml(heading[2].trim()))}</h${level}>`);
      index += 1;
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      const buffer = [];
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
        buffer.push(lines[index].replace(/^\s*>\s?/, ""));
        index += 1;
      }
      html.push(`<blockquote>${renderInline(escapeHtml(buffer.join(" ")))}</blockquote>`);
      continue;
    }

    if (/^\s*[-*+]\s+/.test(line)) {
      const items = [];
      while (index < lines.length && /^\s*[-*+]\s+/.test(lines[index])) {
        items.push(renderInline(escapeHtml(lines[index].replace(/^\s*[-*+]\s+/, ""))));
        index += 1;
      }
      html.push(`<ul>${items.map((item) => `<li>${item}</li>`).join("")}</ul>`);
      continue;
    }

    if (/^\s*\d+[.)]\s+/.test(line)) {
      const items = [];
      while (index < lines.length && /^\s*\d+[.)]\s+/.test(lines[index])) {
        items.push(renderInline(escapeHtml(lines[index].replace(/^\s*\d+[.)]\s+/, ""))));
        index += 1;
      }
      html.push(`<ol>${items.map((item) => `<li>${item}</li>`).join("")}</ol>`);
      continue;
    }

    const buffer = [];
    while (index < lines.length && !startsNewBlock(lines[index])) {
      buffer.push(lines[index]);
      index += 1;
    }
    html.push(`<p>${renderInline(escapeHtml(buffer.join("\n")))}</p>`);
  }

  return html.join("");
}

/** Markdown syntax reads as noise in a one-line list preview, so strip it. */
export function noteExcerpt(note, limit = 150) {
  const source = note?.summary || note?.preview || note?.body || "";
  const plain = String(source)
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/^\s*#{1,6}\s+/gm, "")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/^\s*[-*+]\s+/gm, "")
    .replace(/^\s*\d+[.)]\s+/gm, "")
    .replace(/^\s*>\s?/gm, "")
    .replace(/\s+/g, " ")
    .trim();
  return plain.length > limit ? `${plain.slice(0, limit).trimEnd()}…` : plain;
}

export function formatRelativeTime(iso, now = Date.now()) {
  if (!iso) return "";
  const value = new Date(iso);
  const time = value.getTime();
  if (Number.isNaN(time)) return "";

  const diff = now - time;
  const minute = 60000;
  const hour = 3600000;
  const day = 86400000;

  if (diff < minute) return "just now";
  if (diff < hour) return `${Math.floor(diff / minute)}m ago`;
  if (diff < day) return `${Math.floor(diff / hour)}h ago`;
  if (diff < 7 * day) return `${Math.floor(diff / day)}d ago`;
  return value.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function formatAbsoluteTime(iso) {
  if (!iso) return "";
  const value = new Date(iso);
  if (Number.isNaN(value.getTime())) return "";
  return value.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Splits on commas so a pasted "a, b, c" becomes three chips, and de-duplicates. */
export function parseTagInput(text) {
  const seen = new Set();
  return String(text ?? "")
    .split(",")
    .map((tag) => tag.trim())
    .filter((tag) => {
      if (!tag) return false;
      const key = tag.toLowerCase();
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
}

export function mergeTags(existing, incoming) {
  const merged = [...existing];
  const seen = new Set(existing.map((tag) => tag.toLowerCase()));
  for (const tag of incoming) {
    const key = tag.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    merged.push(tag);
  }
  return merged;
}

export function countWords(text) {
  const trimmed = String(text ?? "").trim();
  return trimmed ? trimmed.split(/\s+/).length : 0;
}
