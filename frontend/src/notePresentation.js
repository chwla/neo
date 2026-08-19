// Notes and chat render the same Markdown subset, so the renderer lives on its own.
// Re-exported here because the note editor has always reached for it through this module.
export { escapeHtml, renderMarkdown } from "./markdown.js";

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
