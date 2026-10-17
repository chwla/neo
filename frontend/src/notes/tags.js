/**
 * Inline #tags, as Apple Notes has them: a tag is something typed in the text.
 *
 * One definition, used both to highlight tags while typing and to collect them
 * when a note is saved, so what looks like a tag is exactly what is filed as one.
 * A tag starts after the start of a line, a space or an opening bracket or quote
 * (so "C#" and "page#anchor" are not tags), and must contain a letter (so "#1" is
 * not one). Normalisation matches the server's: lower case, spaces to hyphens, at
 * most forty characters.
 */
export const TAG_PATTERN = /(^|[\s([{"'])#([\p{L}\p{N}_-]*\p{L}[\p{L}\p{N}_-]*)/gu;

export const MAX_TAG_LENGTH = 40;

export function normalizeTag(tag) {
  return String(tag ?? "")
    .trim()
    .replace(/^#+/, "")
    .toLowerCase()
    .replace(/\s+/g, "-")
    .slice(0, MAX_TAG_LENGTH);
}

/** Every tag in a run of plain text, with its offsets: [{ tag, from, to }]. */
export function findTags(text) {
  const found = [];
  for (const match of String(text ?? "").matchAll(TAG_PATTERN)) {
    const from = match.index + match[1].length;
    found.push({ tag: normalizeTag(match[2]), from, to: from + 1 + match[2].length });
  }
  return found;
}
