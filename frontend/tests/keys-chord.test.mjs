/**
 * The one place a keystroke and a written-down binding have to agree.
 *
 * Every other part of the keyboard system compares chords as plain strings, so a
 * disagreement here is invisible until a binding simply never fires. Three of
 * these pin regressions that are specific to real keyboards rather than to the
 * code: a shifted letter has to normalize the same way whether it arrived as an
 * event or as text in commands.js, a composing input method must not run
 * commands while someone types Japanese, and holding Cmd must not record a chord
 * before the other key is pressed.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import {
  RESERVED_CHORDS,
  UNPREVENTABLE_CHORDS,
  chordFromEvent,
  formatChord,
  formatSequence,
  normalizeChord,
  parseSequence,
} from "../src/keys/chord.js";

/** A keydown with every flag off unless the test says otherwise. */
function press(key, flags = {}) {
  return { key, altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, ...flags };
}

describe("a chord read off a keydown", () => {
  test("a bare letter is itself", () => {
    assert.equal(chordFromEvent(press("j")), "j");
  });

  test("a shifted letter is the capital, not shift plus the lowercase", () => {
    // The capital is what the event actually reports, and it is the only spelling
    // that survives a layout where the shifted character is not a letter at all.
    assert.equal(chordFromEvent(press("G", { shiftKey: true })), "G");
  });

  test("a punctuation key is the character it produced, whatever produced it", () => {
    // "?" is Shift+/ on a US keyboard and Shift+ß on a German one. Both report "?".
    assert.equal(chordFromEvent(press("?", { shiftKey: true })), "?");
    assert.equal(chordFromEvent(press("/")), "/");
  });

  test("a hard modifier switches to the spelled-out form with the key lowercased", () => {
    // macOS reports event.key as "K" here, so the literal character is unusable.
    assert.equal(chordFromEvent(press("K", { metaKey: true, shiftKey: true })), "meta+shift+k");
  });

  test("modifiers come out in one order no matter how they were held", () => {
    const flags = { altKey: true, ctrlKey: true, metaKey: true, shiftKey: true };
    assert.equal(chordFromEvent(press("k", flags)), "alt+ctrl+meta+shift+k");
  });

  test("named keys are lowercased and aliased", () => {
    assert.equal(chordFromEvent(press("Escape")), "escape");
    assert.equal(chordFromEvent(press("ArrowUp")), "up");
    assert.equal(chordFromEvent(press("ArrowDown")), "down");
    assert.equal(chordFromEvent(press("ArrowLeft")), "left");
    assert.equal(chordFromEvent(press("ArrowRight")), "right");
    assert.equal(chordFromEvent(press("PageDown")), "pagedown");
    assert.equal(chordFromEvent(press("Enter")), "enter");
  });

  test("space is named rather than left as a blank", () => {
    // A chord of " " would be indistinguishable from an empty one everywhere else.
    assert.equal(chordFromEvent(press(" ")), "space");
    assert.equal(chordFromEvent(press(" ", { ctrlKey: true })), "ctrl+space");
  });

  test("shift plus a named key keeps the shift, because there is no shifted spelling", () => {
    assert.equal(chordFromEvent(press("Tab", { shiftKey: true })), "shift+tab");
  });

  test("holding a modifier on its own records nothing", () => {
    for (const key of ["Shift", "Control", "Alt", "Meta", "CapsLock", "AltGraph", "OS"]) {
      assert.equal(chordFromEvent(press(key, { metaKey: true })), null, key);
    }
  });

  test("a composing input method is left entirely alone", () => {
    // Without this the whole keymap fires underneath someone typing Japanese.
    assert.equal(chordFromEvent(press("a", { isComposing: true })), null);
    assert.equal(chordFromEvent({ ...press("a"), keyCode: 229 }), null);
  });

  test("a half-finished character is not a key", () => {
    for (const key of ["Dead", "Process", "Unidentified"]) {
      assert.equal(chordFromEvent(press(key)), null, key);
    }
  });

  test("a missing or malformed event is ignored rather than thrown on", () => {
    assert.equal(chordFromEvent(undefined), null);
    assert.equal(chordFromEvent({}), null);
    assert.equal(chordFromEvent(press("")), null);
  });
});

describe("a chord someone wrote down", () => {
  test("normalizes to exactly what the same keystroke would produce", () => {
    assert.equal(normalizeChord("Cmd+Shift+K"), "meta+shift+k");
    assert.equal(normalizeChord("meta+shift+k"), "meta+shift+k");
    assert.equal(chordFromEvent(press("K", { metaKey: true, shiftKey: true })), "meta+shift+k");
  });

  test("accepts the spellings a person reaches for", () => {
    assert.equal(normalizeChord("Control+ArrowUp"), "ctrl+up");
    assert.equal(normalizeChord("option+j"), "alt+j");
    assert.equal(normalizeChord("Command+,"), "meta+,");
  });

  test("orders modifiers regardless of how they were authored", () => {
    assert.equal(normalizeChord("Shift+Ctrl+K"), "ctrl+shift+k");
    assert.equal(normalizeChord("Ctrl+Shift+K"), "ctrl+shift+k");
  });

  test("folds shift over a letter into the capital", () => {
    // Otherwise commands.js could hold a binding no keystroke can ever match.
    assert.equal(normalizeChord("shift+g"), "G");
    assert.equal(normalizeChord("G"), "G");
  });

  test("leaves mod unresolved, for the keymap to expand per platform", () => {
    assert.equal(normalizeChord("mod+k"), "mod+k");
    assert.equal(normalizeChord("Mod+Shift+O"), "mod+shift+o");
  });

  test("treats a lone plus as a key, not as a separator", () => {
    assert.equal(normalizeChord("mod++"), "mod++");
    assert.equal(normalizeChord("+"), "+");
  });

  test("refuses input that names no key rather than inventing one", () => {
    assert.equal(normalizeChord(""), "");
    assert.equal(normalizeChord("   "), "");
    assert.equal(normalizeChord("notamodifier+k"), "");
    assert.equal(normalizeChord(undefined), "");
  });
});

describe("sequences", () => {
  test("split on whitespace and normalize each chord", () => {
    assert.deepEqual(parseSequence("g c"), ["g", "c"]);
    assert.deepEqual(parseSequence("d d"), ["d", "d"]);
    assert.deepEqual(parseSequence("Cmd+K"), ["meta+k"]);
  });

  test("collapse extra whitespace", () => {
    assert.deepEqual(parseSequence("  g    c  "), ["g", "c"]);
  });

  test("drop chords that normalize away, leaving the rest usable", () => {
    assert.deepEqual(parseSequence("g notamodifier+c"), ["g"]);
    assert.deepEqual(parseSequence(""), []);
    assert.deepEqual(parseSequence(undefined), []);
  });
});

describe("how a chord is drawn", () => {
  test("a Mac draws modifiers as symbols with no joiner", () => {
    assert.equal(formatChord("meta+shift+k", "mac"), "⌘⇧K");
    assert.equal(formatChord("alt+ctrl+j", "mac"), "⌥⌃J");
  });

  test("everywhere else draws them as words joined by plus", () => {
    assert.equal(formatChord("meta+shift+k", "other"), "Win+Shift+K");
    assert.equal(formatChord("ctrl+shift+k", "other"), "Ctrl+Shift+K");
  });

  test("named keys get a readable label on both platforms", () => {
    assert.equal(formatChord("escape", "mac"), "Esc");
    assert.equal(formatChord("up", "other"), "↑");
    assert.equal(formatChord("pagedown", "other"), "PgDn");
    assert.equal(formatChord("space", "mac"), "Space");
  });

  test("a bare letter is drawn as the capital it is", () => {
    assert.equal(formatChord("j", "mac"), "J");
    assert.equal(formatChord("G", "other"), "G");
  });

  test("a sequence is drawn chord by chord", () => {
    assert.equal(formatSequence("g c", "mac"), "G C");
    assert.equal(formatSequence(["meta+k"], "mac"), "⌘K");
  });

  test("nothing in, nothing out", () => {
    assert.equal(formatChord("", "mac"), "");
    assert.equal(formatSequence("", "mac"), "");
  });
});

describe("the two lists a binding can fall foul of", () => {
  test("reserved chords are the ones that would strand a user", () => {
    // Escape cancels a recording and closes every dialog; Enter sends. Rebinding
    // either leaves no way to undo the rebinding.
    assert.ok(RESERVED_CHORDS.has("escape"));
    assert.ok(RESERVED_CHORDS.has("enter"));
    assert.ok(RESERVED_CHORDS.has("shift+enter"));
  });

  test("every unpreventable chord is stored in canonical form", () => {
    // These are compared against resolved bindings, so a stray "Cmd+N" in the
    // list would silently never match and the guard would pass on nothing.
    for (const chord of UNPREVENTABLE_CHORDS) {
      assert.equal(normalizeChord(chord), chord, chord);
    }
  });

  test("the list covers what the browser takes before the page is told", () => {
    for (const chord of ["mod+n", "mod+t", "mod+w", "mod+1", "mod+9", "mod+shift+t"]) {
      assert.ok(UNPREVENTABLE_CHORDS.has(chord), chord);
    }
  });
});
