/**
 * The quick-key layer: the single letters and `g` sequences that make the
 * keyboard fast, and the rules that keep them from being a nuisance.
 *
 * There is no mode any more. Both of a command's keys are simply bound, and what
 * keeps a bare letter safe is the focus guard rather than a switch -- so the
 * tests that matter here are about the shape of the default set: that the fast
 * keys exist, that they are all distinct, and that none of them has leaked into
 * the slot that works while somebody is typing.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { COMMANDS } from "../src/keys/commands.js";
import { bindingsFor, buildKeymap } from "../src/keys/keymap.js";

const keymap = buildKeymap(COMMANDS, [], { platform: "mac" });
const slotKey = (id, slot) =>
  bindingsFor(keymap, id).find((binding) => binding.slot === slot)?.key ?? "";

describe("a command's two keys", () => {
  test("are both bound at once, with nothing to switch between them", () => {
    assert.equal(slotKey("app.openSettings", "primary"), "meta+,");
    assert.equal(slotKey("app.openSettings", "alternate"), "g s");
    assert.equal(slotKey("app.toggleSidebar", "primary"), "meta+b");
    assert.equal(slotKey("app.toggleSidebar", "alternate"), "\\");
  });

  test("neither one costs the other", () => {
    // The regression this pins: the quick key used to replace the shortcut, so a
    // command that gained "g s" lost mod+, without anybody asking for that.
    for (const command of COMMANDS) {
      if (!command.altKeys || !command.keys) continue;
      assert.notEqual(
        slotKey(command.id, "primary"), "",
        `${command.id} lost its shortcut to its quick key`,
      );
      assert.notEqual(
        slotKey(command.id, "alternate"), "",
        `${command.id} lost its quick key`,
      );
    }
  });

  test("a command may have only one, and that is not an error", () => {
    assert.equal(slotKey("palette.open", "primary"), "meta+k");
    assert.equal(slotKey("palette.open", "alternate"), "", "no quick key, and none invented");
  });
});

describe("the g namespace", () => {
  const navigation = COMMANDS.filter((entry) => entry.section === "Navigation");

  test("reaches every screen", () => {
    assert.ok(navigation.length >= 12);
    for (const entry of navigation) {
      assert.match(slotKey(entry.id, "alternate"), /^g \S+$/, entry.id);
    }
  });

  test("spends no modifier chord on a screen a click already reaches", () => {
    for (const entry of navigation) {
      assert.equal(slotKey(entry.id, "primary"), "", `${entry.id} should be quick-key only`);
    }
  });

  test("is distinct throughout, or one of them would be unreachable", () => {
    const keys = navigation.map((entry) => slotKey(entry.id, "alternate"));
    assert.equal(new Set(keys).size, keys.length);
  });

  test("nothing binds a bare g, which would swallow all of it", () => {
    for (const binding of keymap.bindings) {
      assert.notEqual(binding.key, "g", `${binding.id} would shadow the whole g namespace`);
    }
  });
});

describe("motions", () => {
  test("are on the letters that mean them, and take a repeat", () => {
    assert.equal(slotKey("chat.scrollDown", "alternate"), "j");
    assert.equal(slotKey("chat.scrollUp", "alternate"), "k");
    assert.equal(slotKey("chat.scrollTop", "alternate"), "g g");
    assert.equal(slotKey("chat.scrollBottom", "alternate"), "G");

    for (const id of ["chat.scrollDown", "chat.scrollUp", "chat.pageDown", "chat.pageUp"]) {
      const binding = bindingsFor(keymap, id).find((entry) => entry.slot === "alternate");
      assert.equal(binding.repeatable, true, id);
    }
  });

  test("keep an arrow-key equivalent for anybody who wants one", () => {
    assert.equal(slotKey("chat.scrollDown", "primary"), "down");
    assert.equal(slotKey("chat.pageDown", "primary"), "pagedown");
  });
});

describe("the composer", () => {
  test("has a way back into it, on a letter and on a chord", () => {
    assert.equal(slotKey("chat.focusComposer", "primary"), "meta+i");
    assert.equal(slotKey("chat.focusComposer", "alternate"), "i");
  });

  test("is left by Escape, which the engine owns rather than the keymap", () => {
    const leave = COMMANDS.find((entry) => entry.id === "composer.leave");
    assert.ok(leave, "the command is still listed, so the key is discoverable");
    assert.equal(leave.fixed, true, "and not rebindable, being the way out of everything");
  });

  test("has no modal-editor entry variants left", () => {
    // i/a/A/I/o only meant anything as ways into an insert mode. With one mode
    // there is just "focus the composer".
    for (const id of ["mode.type", "mode.typeAfter", "mode.typeEnd", "mode.typeStart"]) {
      assert.equal(COMMANDS.find((entry) => entry.id === id), undefined, id);
    }
  });
});

describe("nothing named after a mode survives", () => {
  test("no command id or field mentions one", () => {
    for (const command of COMMANDS) {
      assert.ok(!command.id.startsWith("mode."), `${command.id} is named after a mode`);
      assert.equal(command.commandKeys, undefined, `${command.id} still has commandKeys`);
    }
    assert.equal(COMMANDS.find((entry) => entry.id === "app.toggleCommandMode"), undefined);
  });
});
