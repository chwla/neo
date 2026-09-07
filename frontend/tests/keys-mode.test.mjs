/**
 * The guard that decides whether a bare key is a command or a letter.
 *
 * The headline property is at the bottom and it is the reason the feature is
 * safe to ship on by default: over the real catalogue, in both modes, no bare
 * default fires while a text field has focus. Everything above it exists to make
 * that hold -- one classifier instead of the three different regexes the app had
 * grown, and a mode derived from focus rather than remembered, so there is no
 * stored state anybody can be stranded in.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { COMMANDS } from "../src/keys/commands.js";
import { createDispatcher } from "../src/keys/dispatcher.js";
import { buildKeymap } from "../src/keys/keymap.js";
import { allowsBareKeys, deriveMode, focusKind } from "../src/keys/mode.js";

const element = (tagName, extra = {}) => ({ tagName, ...extra });

describe("what has focus", () => {
  test("a textarea is text", () => {
    assert.equal(focusKind(element("TEXTAREA")), "text");
  });

  test("an input is text or control depending on what it holds", () => {
    for (const type of ["text", "search", "email", "url", "tel", "password", "number"]) {
      assert.equal(focusKind(element("INPUT", { type })), "text", type);
    }
    for (const type of ["checkbox", "radio", "range", "file", "color", "submit"]) {
      assert.equal(focusKind(element("INPUT", { type })), "control", type);
    }
  });

  test("an input with no type is text, which is the HTML default", () => {
    assert.equal(focusKind(element("INPUT")), "text");
  });

  test("a date field counts as text, because its own keys include bare digits", () => {
    assert.equal(focusKind(element("INPUT", { type: "date" })), "text");
  });

  test("contenteditable is text, which none of the old regexes knew", () => {
    assert.equal(focusKind(element("DIV", { isContentEditable: true })), "text");
    assert.equal(focusKind(element("DIV", { getAttribute: () => "true" })), "text");
    assert.equal(focusKind(element("DIV", { getAttribute: () => "plaintext-only" })), "text");
    assert.equal(focusKind(element("DIV", { getAttribute: () => null })), "none");
  });

  test("a select or a button is a control, not text and not nothing", () => {
    // A select needs j and k for its own options, but has no claim on mod+k.
    assert.equal(focusKind(element("SELECT")), "control");
    assert.equal(focusKind(element("BUTTON")), "control");
  });

  test("anything else is nothing", () => {
    assert.equal(focusKind(element("DIV")), "none");
    assert.equal(focusKind(element("BODY")), "none");
    assert.equal(focusKind(null), "none");
    assert.equal(focusKind(undefined), "none");
  });

  test("only nothing lets a bare key act", () => {
    assert.equal(allowsBareKeys("none"), true);
    assert.equal(allowsBareKeys("text"), false);
    assert.equal(allowsBareKeys("control"), false);
  });
});

describe("the mode, derived rather than remembered", () => {
  test("text focus is Typing, everything else is Command", () => {
    assert.equal(deriveMode("text"), "typing");
    assert.equal(deriveMode("control"), "command");
    assert.equal(deriveMode("none"), "command");
  });

  test("focusing a text field yields Typing regardless of anything prior", () => {
    // There is no prior. That is the point -- the argument is that the function
    // takes only focus, so no history can produce a different answer.
    assert.equal(deriveMode.length, 1);
    assert.equal(deriveMode("text"), "typing");
  });
});

describe("no bare default can fire while somebody is typing", () => {
  const press = (chord) => {
    const parts = chord.split("+");
    const key = parts[parts.length - 1];
    return {
      key: key.length === 1 ? key : key,
      altKey: parts.includes("alt"),
      ctrlKey: parts.includes("ctrl"),
      metaKey: parts.includes("meta"),
      shiftKey: parts.includes("shift"),
    };
  };

  for (const commandMode of [false, true]) {
    const label = commandMode ? "with Command mode on" : "with Command mode off";

    test(`${label}, every bare default is inert in a text field`, () => {
      const keymap = buildKeymap(COMMANDS, [], { platform: "mac", commandMode });
      const context = { scopes: new Set(["chat", "notes", "gallery"]), focusKind: "text" };

      for (const binding of keymap.bindings) {
        for (const chord of binding.chords) {
          if (/ctrl\+|alt\+|meta\+/.test(chord)) continue;
          const dispatcher = createDispatcher({ keymap, commandMode });
          const result = dispatcher.feed(press(chord), context);
          assert.equal(
            result.action,
            "none",
            `${binding.id} acted on bare ${chord} while the user was typing`,
          );
        }
      }
    });

    test(`${label}, a modifier chord still works from a control`, () => {
      // A focused <select> swallows j and k for its own options, but there is no
      // reason for it to eat the command palette.
      const keymap = buildKeymap(COMMANDS, [], { platform: "mac", commandMode });
      const dispatcher = createDispatcher({ keymap, commandMode });
      const context = { scopes: new Set(["chat"]), focusKind: "control" };

      assert.equal(dispatcher.feed(press("meta+k"), context).commandId, "palette.open");
      assert.equal(dispatcher.feed({ key: "j" }, context).action, "none");
    });
  }

  test("a half-typed sequence is dropped when focus lands in a text field", () => {
    // Otherwise coming back to the page resumes a buffer from minutes ago.
    const keymap = buildKeymap(COMMANDS, [], { platform: "mac", commandMode: true });
    const dispatcher = createDispatcher({ keymap, commandMode: true });

    assert.equal(dispatcher.feed({ key: "g" }, { scopes: new Set(), focusKind: "none" }).action, "pending");
    dispatcher.feed({ key: "n" }, { scopes: new Set(), focusKind: "text" });
    assert.deepEqual(dispatcher.pending, []);
  });
});
