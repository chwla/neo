/**
 * Putting dictated text into a box somebody may also be typing in.
 *
 * The server returns trimmed text with no opinion about spacing, because whether a
 * leading space is wanted depends on the character before the caret and only the
 * browser knows that. This module owns that decision, plus the arithmetic for
 * replacing a provisional span as successive partials refine it.
 *
 * Everything here is a pure function over strings and offsets, which is what lets the
 * behaviour be tested without a DOM -- the frontend suite has no jsdom.
 */

/** Characters after which a space would be wrong. */
const OPENS = new Set(["(", "[", "{", "“", '"', "'", "-", "/", "\n", "\t"]);

/** Characters before which a space would be wrong. */
const CLOSES = new Set([".", ",", "!", "?", ";", ":", ")", "]", "}", "”"]);

/**
 * How the incoming text should be glued to what surrounds it.
 *
 * Returns the text with any needed leading and trailing space already attached, so the
 * caller only has to splice it in.
 */
export function spaceFor(text, before, after) {
  const body = (text ?? "").trim();
  if (!body) return "";

  const previous = before.slice(-1);
  const next = after.slice(0, 1);

  const needsLeading =
    previous !== "" && !/\s/.test(previous) && !OPENS.has(previous) && !CLOSES.has(body[0]);
  const needsTrailing =
    next !== "" && !/\s/.test(next) && !CLOSES.has(next) && !OPENS.has(body[body.length - 1]);

  return `${needsLeading ? " " : ""}${body}${needsTrailing ? " " : ""}`;
}

/**
 * Replace a span of `value` with `text`, reporting where the caret and the new span end up.
 *
 * Offsets are clamped rather than trusted: a provisional range recorded before an
 * asynchronous partial arrived can point past the end of a value the user has since
 * shortened, and a silent clamp is better than a thrown exception in the middle of
 * somebody dictating.
 */
export function replaceRange(value, range, text) {
  const current = value ?? "";
  const start = Math.max(0, Math.min(range?.start ?? current.length, current.length));
  const end = Math.max(start, Math.min(range?.end ?? start, current.length));

  const spaced = spaceFor(text, current.slice(0, start), current.slice(end));
  const next = current.slice(0, start) + spaced + current.slice(end);

  return {
    value: next,
    caret: start + spaced.length,
    range: { start, end: start + spaced.length },
  };
}

/**
 * Whether the box changed under us because the user typed.
 *
 * Dictation writes through the same controlled value the keyboard does, so the only
 * way to tell the two apart is to remember what we last wrote. Anything else is the
 * user, and when it is the user we stop tracking our span rather than trying to rebase
 * an incoming partial onto their edit -- that way lies a bug farm, and "you touched
 * it, so I stop touching it" is predictable every time.
 */
export function wasEditedByUser(currentValue, lastWrittenByUs) {
  return (currentValue ?? "") !== (lastWrittenByUs ?? "");
}
