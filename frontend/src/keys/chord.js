/**
 * Turning keyboard events and authored strings into one canonical chord form.
 *
 * Everything downstream -- the keymap, the dispatcher, the settings screen --
 * compares chords as plain strings, so the single job here is that a key pressed
 * by a user and the same key written into `commands.js` produce the identical
 * string. Two rules carry that, and they are the reason this file is not a
 * one-liner.
 *
 * A bare key with no hard modifier is stored as the character it produced, with
 * shift already spent: Shift+G is "G", not "shift+g". That is what makes single
 * key bindings survive a non-US layout, where "?" is Shift+7 or Shift+' and the
 * only thing both keyboards agree on is that `event.key` came out "?".
 *
 * Add ctrl, alt or meta and the character stops being reliable -- macOS reports
 * `event.key` as "K" for Cmd+Shift+K, and as "ç" for Alt+C -- so those chords are
 * spelled out from the modifier flags with the key lowercased instead.
 *
 * `mod` is left unresolved here on purpose. It becomes meta or ctrl once, when the
 * keymap is built for a platform, which keeps this file free of platform branching
 * and lets one test build both keymaps from a single authored table.
 */

/** Modifier tokens in the order a canonical chord spells them. */
const MODIFIER_ORDER = ["alt", "ctrl", "meta", "mod", "shift"];

/** Spellings accepted from authored strings, mapped to the canonical token. */
const MODIFIER_ALIASES = {
  alt: "alt",
  opt: "alt",
  option: "alt",
  ctrl: "ctrl",
  control: "ctrl",
  meta: "meta",
  cmd: "meta",
  command: "meta",
  super: "meta",
  win: "meta",
  mod: "mod",
  shift: "shift",
};

/**
 * Keys that only ever appear alongside another key. A keydown for one of these is
 * the user reaching for a modifier, not pressing anything, and recording it would
 * capture a chord the moment they touched Cmd.
 */
const PURE_MODIFIERS = new Set([
  "Alt", "AltGraph", "CapsLock", "Control", "Fn", "FnLock", "Hyper", "Meta",
  "NumLock", "OS", "ScrollLock", "Shift", "Super", "Symbol", "SymbolLock",
]);

/**
 * Keys that carry no press of their own. "Dead" is a pending accent, "Process"
 * and "Unidentified" are what a browser reports when an input method owns the
 * keystroke -- all three would otherwise become bindable chords named after a
 * half-finished character.
 */
const NON_KEYS = new Set(["Dead", "Process", "Unidentified"]);

/** Named keys, mapped to the lowercase token a chord spells them with. */
const KEY_ALIASES = {
  " ": "space",
  spacebar: "space",
  escape: "escape",
  esc: "escape",
  arrowup: "up",
  arrowdown: "down",
  arrowleft: "left",
  arrowright: "right",
  pageup: "pageup",
  pagedown: "pagedown",
  del: "delete",
};

/** How each token is drawn on a Mac, where modifiers are symbols with no joiner. */
const MAC_SYMBOLS = { alt: "⌥", ctrl: "⌃", meta: "⌘", shift: "⇧" };

/** How each token is drawn everywhere else, where modifiers are words joined by "+". */
const PC_WORDS = { alt: "Alt", ctrl: "Ctrl", meta: "Win", shift: "Shift" };

/** Named keys as they are drawn to a user, for both platforms. */
const KEY_LABELS = {
  space: "Space", escape: "Esc", enter: "Enter", tab: "Tab", backspace: "Backspace",
  delete: "Delete", home: "Home", end: "End", pageup: "PgUp", pagedown: "PgDn",
  up: "↑", down: "↓", left: "←", right: "→",
};

/**
 * Chords the engine will not let a user take. Escape is how a recording is
 * cancelled and how every dialog closes, and Enter in a text field is how a
 * message is sent -- rebinding either leaves no way to undo the rebinding.
 *
 * Shift+Enter is here for a subtler reason: the composer's own handler calls
 * preventDefault only for a bare Enter, so Shift+Enter is the one keystroke that
 * reaches the engine live while the user is mid-sentence.
 */
export const RESERVED_CHORDS = new Set(["escape", "enter", "shift+enter"]);

/**
 * Chords the browser eats before the page is told. Chrome and Safari on macOS act
 * on all of these in the browser itself, so a binding here is not merely
 * overridden -- it never runs, and the user is left thinking the feature is broken.
 * Notes has shipped a dead Cmd+N "new note" binding on this list for months.
 *
 * Authored with `mod`, and checked after the keymap resolves it, so the same list
 * covers Cmd on macOS and Ctrl elsewhere.
 */
export const UNPREVENTABLE_CHORDS = new Set([
  "mod+n", "mod+t", "mod+w", "mod+q", "mod+m", "mod+h",
  "mod+shift+n", "mod+shift+t",
  "mod+1", "mod+2", "mod+3", "mod+4", "mod+5",
  "mod+6", "mod+7", "mod+8", "mod+9",
]);

/**
 * Splits on "+" without swallowing "+" as a key. An empty run means the separator
 * was itself the key, so "mod++" is meta plus the plus key rather than a modifier
 * and nothing.
 */
function splitChord(text) {
  const parts = [];
  let current = "";
  for (const character of text) {
    if (character === "+" && current !== "") {
      parts.push(current);
      current = "";
    } else {
      current += character;
    }
  }
  parts.push(current);
  return parts.filter((part) => part !== "");
}

function aliasKey(key) {
  const lower = key.toLowerCase();
  return KEY_ALIASES[lower] ?? lower;
}

function orderModifiers(modifiers) {
  return MODIFIER_ORDER.filter((token) => modifiers.has(token));
}

/**
 * The canonical chord for a keydown, or null when the event is not a press worth
 * binding. Callers treat null as "ignore this event entirely" rather than as an
 * error -- it is the ordinary result of holding a modifier down.
 */
export function chordFromEvent(event) {
  if (!event || typeof event.key !== "string" || event.key === "") {
    return null;
  }
  // An input method owns this keystroke. Without this guard, every keypress that
  // composes a Japanese or accented character also runs whatever it is bound to.
  if (event.isComposing || event.keyCode === 229) {
    return null;
  }
  if (PURE_MODIFIERS.has(event.key) || NON_KEYS.has(event.key)) {
    return null;
  }

  const hard = Boolean(event.ctrlKey || event.altKey || event.metaKey);
  if (!hard && event.key.length === 1 && event.key !== " ") {
    return event.key;
  }

  const modifiers = new Set();
  if (event.altKey) modifiers.add("alt");
  if (event.ctrlKey) modifiers.add("ctrl");
  if (event.metaKey) modifiers.add("meta");
  if (event.shiftKey) modifiers.add("shift");
  return [...orderModifiers(modifiers), aliasKey(event.key)].join("+");
}

/**
 * The canonical form of a chord someone wrote down -- a default in `commands.js`,
 * or a sequence read back from a profile's stored overrides. Accepts the spellings
 * a human would reach for ("Cmd+Shift+K", "ctrl+ArrowUp") and returns what
 * `chordFromEvent` would have produced for the same keystroke.
 *
 * Returns "" for input that names no key, so a caller can drop it rather than
 * build a binding that can never match.
 */
export function normalizeChord(text) {
  if (typeof text !== "string") {
    return "";
  }
  const parts = splitChord(text.trim());
  if (parts.length === 0) {
    return "";
  }

  const key = parts[parts.length - 1];
  const modifiers = new Set();
  for (const part of parts.slice(0, -1)) {
    const token = MODIFIER_ALIASES[part.toLowerCase()];
    if (!token) {
      return "";
    }
    modifiers.add(token);
  }

  const hard = modifiers.has("alt") || modifiers.has("ctrl")
    || modifiers.has("meta") || modifiers.has("mod");

  // Shift alone over a letter is the same keystroke as the capital, and only one
  // of the two spellings can ever match an event. Fold it into the capital so an
  // authored "shift+g" and a recorded "G" are the same binding.
  if (!hard && modifiers.has("shift") && /^[a-z]$/i.test(key)) {
    return key.toUpperCase();
  }
  if (!hard && modifiers.size === 0 && key.length === 1 && key !== " ") {
    return key;
  }
  return [...orderModifiers(modifiers), aliasKey(key)].join("+");
}

/**
 * A whitespace-separated binding -- "g c", "d d", "mod+k" -- as an array of
 * canonical chords. Anything that normalizes away is dropped, so a malformed
 * override becomes a shorter sequence or an empty one rather than a broken entry.
 */
export function parseSequence(text) {
  if (typeof text !== "string") {
    return [];
  }
  return text
    .trim()
    .split(/\s+/)
    .map(normalizeChord)
    .filter((chord) => chord !== "");
}

/** The canonical string for a sequence, and the form stored in the database. */
export function formatSequenceKey(chords) {
  return chords.join(" ");
}

/** One chord as it is drawn to a user. `platform` is "mac" or anything else. */
export function formatChord(chord, platform = "other") {
  const chords = normalizeChord(chord);
  if (chords === "") {
    return "";
  }
  const parts = splitChord(chords);
  const key = parts[parts.length - 1];
  const modifiers = parts.slice(0, -1);
  const mac = platform === "mac";

  const label = KEY_LABELS[key] ?? (key.length === 1 ? key.toUpperCase() : key);
  const drawn = modifiers.map((token) => (mac ? MAC_SYMBOLS[token] : PC_WORDS[token]) ?? token);
  return mac ? drawn.join("") + label : [...drawn, label].join("+");
}

/** A whole sequence as it is drawn to a user, chords separated by a space. */
export function formatSequence(sequence, platform = "other") {
  const chords = Array.isArray(sequence) ? sequence : parseSequence(sequence);
  return chords.map((chord) => formatChord(chord, platform)).join(" ");
}
