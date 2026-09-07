/**
 * What Command mode actually adds, and what it must never take away.
 *
 * The indicator is tested for one specific string. Mode is derived from focus
 * rather than stored, so nobody is stuck in any real sense -- but somebody who
 * pressed a key and watched the page scroll instead of typing does not know
 * that, and "i to type" is the whole difference between a surprise and a mode.
 * If it goes, this fails.
 *
 * The rest pins the shape of the two keymaps against each other: the standard
 * one must not have grown a bare letter, and the command one must reach every
 * screen through g.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import KeyboardModeIndicator from "../src/KeyboardModeIndicator.jsx";
import { COMMANDS } from "../src/keys/commands.js";
import { bindingsFor, buildKeymap } from "../src/keys/keymap.js";

const standard = buildKeymap(COMMANDS, [], { platform: "mac", commandMode: false });
const command = buildKeymap(COMMANDS, [], { platform: "mac", commandMode: true });
const slotKey = (keymap, id, slot) =>
  bindingsFor(keymap, id).find((binding) => binding.slot === slot)?.key ?? "";
const keyOf = (keymap, id) => slotKey(keymap, id, "command") || slotKey(keymap, id, "standard");

describe("the mode indicator", () => {
  const render = (props) =>
    renderToStaticMarkup(createElement(KeyboardModeIndicator, props));

  test("is not on screen at all until Command mode is turned on", () => {
    assert.equal(render({ enabled: false }), "");
  });

  test("says which mode you are in, and how to leave it", () => {
    const markup = render({ enabled: true });
    assert.ok(markup.includes("COMMAND"));
    assert.ok(markup.includes("i to type"), "the way out has to be on screen");
  });
});

describe("what Command mode adds", () => {
  test("a way into the composer, which the standard keymap has no letter for", () => {
    assert.equal(keyOf(command, "mode.type"), "i");
    assert.equal(keyOf(standard, "mode.type"), "");
    assert.equal(keyOf(command, "mode.typeEnd"), "A");
    assert.equal(keyOf(command, "mode.typeStart"), "I");
  });

  test("the g namespace, reaching every screen", () => {
    const navigation = COMMANDS.filter((entry) => entry.section === "Navigation");
    assert.ok(navigation.length >= 12);

    for (const entry of navigation) {
      const key = keyOf(command, entry.id);
      assert.match(key, /^g \S+$/, `${entry.id} is bound to ${key || "nothing"}`);
      assert.equal(keyOf(standard, entry.id), "", `${entry.id} should be Command mode only`);
    }
  });

  test("every g binding is distinct, or one of them would be unreachable", () => {
    const keys = COMMANDS
      .filter((entry) => entry.section === "Navigation")
      .map((entry) => keyOf(command, entry.id));
    assert.equal(new Set(keys).size, keys.length);
  });

  test("motions, which is what a count is for", () => {
    assert.equal(keyOf(command, "chat.scrollDown"), "j");
    assert.equal(keyOf(command, "chat.scrollUp"), "k");
    assert.equal(keyOf(command, "chat.scrollTop"), "g g");
    assert.equal(keyOf(command, "chat.scrollBottom"), "G");
    for (const id of ["chat.scrollDown", "chat.scrollUp", "chat.pageDown", "chat.pageUp"]) {
      assert.equal(bindingsFor(command, id)[0].repeatable, true, id);
    }
  });

  test("strictly more than the standard keymap, never less", () => {
    // The regression this pins: commandKeys used to *replace* the standard
    // binding rather than join it, so turning Command mode on killed mod+, for
    // Settings and mod+shift+O for New chat. Enabling a feature must never take
    // a shortcut away from somebody whose fingers already know it.
    for (const entry of COMMANDS) {
      const before = slotKey(standard, entry.id, "standard");
      if (before === "") continue;
      assert.equal(
        slotKey(command, entry.id, "standard"),
        before,
        `${entry.id} lost ${before} when Command mode was turned on`,
      );
    }
  });

  test("both keys reach the same command where it has two", () => {
    assert.equal(slotKey(command, "app.openSettings", "standard"), "meta+,");
    assert.equal(slotKey(command, "app.openSettings", "command"), "g s");
  });
});

describe("what the standard keymap must never become", () => {
  test("no bare letter, so a letter always types", () => {
    for (const binding of standard.bindings) {
      for (const chord of binding.chords) {
        assert.ok(!/^[a-zA-Z]$/.test(chord), `${binding.id} binds bare ${chord}`);
      }
    }
  });

  test("no digit, which would make counts ambiguous if it were ever shared", () => {
    for (const binding of standard.bindings) {
      for (const chord of binding.chords) {
        assert.ok(!/^[0-9]$/.test(chord), `${binding.id} binds bare ${chord}`);
      }
    }
  });

  test("the toggle into Command mode is reachable without one", () => {
    // It ships unbound on purpose, so the palette and the settings screen are
    // the only ways in -- and both must therefore work with no keyboard at all.
    assert.equal(keyOf(standard, "app.toggleCommandMode"), "");
    assert.equal(keyOf(command, "app.toggleCommandMode"), "");
    assert.equal(keyOf(standard, "palette.open"), "meta+k");
  });
});
