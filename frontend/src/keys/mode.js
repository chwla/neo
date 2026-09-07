/**
 * What has focus, and therefore which mode we are in.
 *
 * The mode is derived, never stored. That is the whole safety argument for this
 * feature: Command mode means "the composer is blurred", so there is no state to
 * get stuck in and no sequence of keystrokes that can leave someone unable to
 * type. Clicking any text field puts you back in Typing whether Command mode is
 * on or off, because the answer is recomputed from focus every time.
 *
 * Turning Command mode on adds transitions and bindings. It does not add a second
 * source of truth.
 *
 * This also replaces three different answers to "is the user typing?" that were
 * scattered around the app -- /^(INPUT|TEXTAREA)$/ in Notes,
 * /^(INPUT|TEXTAREA|SELECT)$/ in Gallery, and nothing at all in the lightbox --
 * none of which knew about contenteditable.
 */

/**
 * Input types that accept free text. An <input> with no type is one of these:
 * "text" is the HTML default.
 *
 * The date and time types are here even though they are not free text, because
 * their own key handling includes bare digits and arrows, and losing those to a
 * command would be worse than missing a shortcut.
 */
const TEXT_INPUT_TYPES = new Set([
  "text", "search", "email", "url", "tel", "password", "number",
  "date", "datetime-local", "month", "time", "week",
]);

/**
 * Focusable things that consume keys of their own without being text. A <select>
 * must keep j and k -- they jump through its options -- but has no reason to
 * swallow mod+k, which is why this is a third answer rather than a second.
 */
const CONTROL_TAGS = new Set(["SELECT", "BUTTON", "OPTION"]);

function isContentEditable(target) {
  if (target.isContentEditable) {
    return true;
  }
  const attribute = target.getAttribute?.("contenteditable");
  return attribute === "" || attribute === "true" || attribute === "plaintext-only";
}

/**
 * "text", "control" or "none" for whatever currently has focus. Anything
 * unrecognised is "none", which is the permissive answer -- but the recognised
 * set covers every element that can take a keystroke, so unrecognised means the
 * body or a div.
 */
export function focusKind(target) {
  if (!target || typeof target !== "object") {
    return "none";
  }
  const tag = String(target.tagName ?? "").toUpperCase();

  if (tag === "TEXTAREA") {
    return "text";
  }
  if (tag === "INPUT") {
    const type = String(target.type ?? "text").toLowerCase();
    return TEXT_INPUT_TYPES.has(type) ? "text" : "control";
  }
  if (isContentEditable(target)) {
    return "text";
  }
  if (CONTROL_TAGS.has(tag)) {
    return "control";
  }
  return "none";
}

/**
 * The mode, from focus alone. Identical whether Command mode is on or off -- the
 * toggle changes what the modes do, not how you end up in one.
 */
export function deriveMode(kind) {
  return kind === "text" ? "typing" : "command";
}

/**
 * Whether a key with no ctrl, alt or meta may act. Only true when nothing that
 * wants keystrokes has focus, which is what stops a bare "j" from scrolling the
 * transcript while somebody is halfway through a word.
 */
export function allowsBareKeys(kind) {
  return kind === "none";
}
