/**
 * The catalogue's own health, and how overrides are merged onto it.
 *
 * Half of this suite is a guardrail rather than a test of behaviour: adding a
 * command is meant to be one object literal, which only stays true if something
 * else notices when that literal collides with a binding twenty rows away, or
 * claims a chord the browser is going to eat first. Those checks run over the
 * real catalogue on both platforms, so a bad default fails here rather than in
 * somebody's hands.
 *
 * The rest pins the merge: an override replaces exactly one command's binding,
 * a stale id left behind by an upgrade is ignored rather than thrown on, and two
 * screens binding "/" to their own search box are not in conflict.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { UNPREVENTABLE_CHORDS, normalizeChord } from "../src/keys/chord.js";
import { COMMANDS, SCOPES, VIEW_SCOPES, scopesOf } from "../src/keys/commands.js";
import {
  SLOTS,
  bindingsFor,
  buildKeymap,
  findConflicts,
  matchSequence,
  resolveMod,
  resolvedUnpreventable,
  scopesOverlap,
  wouldCollideWith,
} from "../src/keys/keymap.js";

const PLATFORMS = ["mac", "other"];

/** Every keymap the app can actually be running, so a default is checked in both. */
function everyKeymap(overrides = []) {
  return PLATFORMS.map((platform) => buildKeymap(COMMANDS, overrides, { platform }));
}

const command = (id, extra = {}) => ({ id, title: id, section: "Test", keys: "", ...extra });

describe("the catalogue is well formed", () => {
  test("every id is unique", () => {
    const ids = COMMANDS.map((entry) => entry.id);
    assert.equal(new Set(ids).size, ids.length);
  });

  test("every command has a title and a section to file it under", () => {
    for (const entry of COMMANDS) {
      assert.ok(entry.title, entry.id);
      assert.ok(entry.section, entry.id);
      assert.equal(typeof entry.keys, "string", entry.id);
    }
  });

  test("every scope token is one the app can actually produce", () => {
    // A typo here binds a command to a condition that is never true, and the
    // command simply never fires with nothing to show for it.
    for (const entry of COMMANDS) {
      for (const token of scopesOf(entry)) {
        assert.ok(SCOPES.has(token), `${entry.id} declares unknown scope ${token}`);
      }
    }
  });

  test("no command names two views at once", () => {
    // One view shows at a time, so such a command could never run.
    for (const entry of COMMANDS) {
      const views = scopesOf(entry).filter((token) => VIEW_SCOPES.has(token));
      assert.ok(views.length <= 1, `${entry.id} names ${views.join(" and ")}`);
    }
  });
});

describe("the shipped defaults", () => {
  test("collide with nothing, on either platform, in either mode", () => {
    for (const keymap of everyKeymap()) {
      assert.deepEqual(
        findConflicts(keymap),
        [],
        `${keymap.platform} has conflicting defaults`,
      );
    }
  });

  test("never claim a chord the browser takes before the page is told", () => {
    // Cmd+N and friends act in Chrome and Safari themselves. A default here does
    // not lose an argument with the browser -- it never runs at all, which is
    // exactly how Notes shipped a dead "new note" binding.
    for (const keymap of everyKeymap()) {
      const taken = resolvedUnpreventable(keymap.platform);
      for (const binding of keymap.bindings) {
        for (const chord of binding.chords) {
          assert.ok(
            !taken.has(chord),
            `${binding.id} defaults to ${chord}, which ${keymap.platform} browsers eat`,
          );
        }
      }
    }
  });

  test("keep every bare letter in the quick-key slot, never the shortcut slot", () => {
    // The shortcut slot works while you are typing, so a bare letter there would
    // eat a keystroke meant for the composer. The quick-key slot is guarded by
    // focus, which is what makes a letter safe to ship on at all.
    for (const keymap of everyKeymap()) {
      for (const binding of keymap.bindings) {
        if (binding.slot !== "primary") continue;
        for (const chord of binding.chords) {
          assert.ok(!/^[a-zA-Z0-9]$/.test(chord), `${binding.id} has bare ${chord} as its shortcut`);
        }
      }
    }
  });

  test("give a command both of its keys at once, with no mode to switch", () => {
    const keymap = buildKeymap(COMMANDS, [], { platform: "mac" });
    const slots = Object.fromEntries(
      bindingsFor(keymap, "app.openSettings").map((binding) => [binding.slot, binding.key]),
    );
    assert.deepEqual(slots, { primary: "meta+,", alternate: "g s" });
  });
});

describe("mod, resolved once per platform", () => {
  test("becomes the platform's own modifier", () => {
    assert.equal(resolveMod("mod+k", "mac"), "meta+k");
    assert.equal(resolveMod("mod+k", "other"), "ctrl+k");
    assert.equal(resolveMod("mod+shift+o", "other"), "ctrl+shift+o");
  });

  test("leaves a sequence with no mod in it alone", () => {
    assert.equal(resolveMod("g c", "mac"), "g c");
  });

  test("one authored table yields two different keymaps", () => {
    const mac = buildKeymap(COMMANDS, [], { platform: "mac" });
    const pc = buildKeymap(COMMANDS, [], { platform: "other" });
    assert.deepEqual(bindingsFor(mac, "palette.open")[0].chords, ["meta+k"]);
    assert.deepEqual(bindingsFor(pc, "palette.open")[0].chords, ["ctrl+k"]);
  });

  test("the browser-owned list is resolved the same way", () => {
    for (const chord of UNPREVENTABLE_CHORDS) {
      assert.ok(resolvedUnpreventable("mac").has(normalizeChord(resolveMod(chord, "mac"))));
    }
  });
});

describe("merging a profile's overrides", () => {
  const catalogue = [
    command("a.one", { keys: "mod+j" }),
    command("a.two", { keys: "mod+l" }),
  ];

  test("an override replaces exactly one command's binding", () => {
    const keymap = buildKeymap(catalogue, [
      { command_id: "a.one", keymap: "primary", sequence: "mod+z" },
    ], { platform: "mac" });

    assert.deepEqual(bindingsFor(keymap, "a.one")[0].chords, ["meta+z"]);
    assert.equal(bindingsFor(keymap, "a.one")[0].source, "override");
    assert.deepEqual(bindingsFor(keymap, "a.two")[0].chords, ["meta+l"]);
    assert.equal(bindingsFor(keymap, "a.two")[0].source, "default");
  });

  test("an empty sequence is a deliberate unbinding, not a missing override", () => {
    const keymap = buildKeymap(catalogue, [
      { command_id: "a.one", keymap: "primary", sequence: "" },
    ], { platform: "mac" });

    assert.deepEqual(bindingsFor(keymap, "a.one")[0].chords, []);
    assert.equal(keymap.exact.has("meta+j"), false);
  });

  test("an override for the other keymap is left where it belongs", () => {
    const keymap = buildKeymap(catalogue, [
      { command_id: "a.one", keymap: "alternate", sequence: "z" },
    ], { platform: "mac" });

    assert.deepEqual(bindingsFor(keymap, "a.one")[0].chords, ["meta+j"]);
  });

  test("an override for a command that no longer exists is ignored, not thrown on", () => {
    // A profile that has been through an upgrade will have these, and the backend
    // deliberately does not police the ids it stores.
    assert.doesNotThrow(() => {
      const keymap = buildKeymap(catalogue, [
        { command_id: "a.removed", keymap: "primary", sequence: "mod+z" },
      ], { platform: "mac" });
      assert.equal(bindingsFor(keymap, "a.removed").length, 0);
    });
  });

  test("malformed override rows do not take the whole keymap down", () => {
    assert.doesNotThrow(() => buildKeymap(catalogue, [null, {}, { command_id: 7 }], {}));
    assert.doesNotThrow(() => buildKeymap(catalogue, undefined, {}));
  });

  test("a custom quick key leaves the shortcut alone", () => {
    // The regression: the second key used to replace the first, so a user who set
    // "c" for New chat silently lost mod+shift+O. Adding a key must never take
    // one away.
    const keymap = buildKeymap(COMMANDS, [
      { command_id: "chat.new", keymap: "alternate", sequence: "z" },
    ], { platform: "mac" });

    const slots = Object.fromEntries(
      bindingsFor(keymap, "chat.new").map((binding) => [binding.slot, binding.key]),
    );
    assert.equal(slots.primary, "meta+shift+o", "the shortcut survived");
    assert.equal(slots.alternate, "z", "and the custom one was added");
  });

  test("unbinding one slot does not unbind the other", () => {
    const keymap = buildKeymap(COMMANDS, [
      { command_id: "chat.new", keymap: "alternate", sequence: "" },
    ], { platform: "mac" });

    assert.equal(
      bindingsFor(keymap, "chat.new").find((b) => b.slot === "primary").key,
      "meta+shift+o",
    );
  });

  test("a fixed command cannot be overridden", () => {
    const keymap = buildKeymap([command("a.fixed", { keys: "escape", fixed: true })], [
      { command_id: "a.fixed", keymap: "primary", sequence: "mod+z" },
    ], { platform: "mac" });

    assert.deepEqual(bindingsFor(keymap, "a.fixed")[0].chords, ["escape"]);
    assert.equal(keymap.exact.size, 0, "and it is not in the lookup at all");
  });
});

describe("what counts as a conflict", () => {
  test("two commands on the same key in the same scope", () => {
    const keymap = buildKeymap([
      command("a.one", { keys: "mod+j", when: ["chat"] }),
      command("a.two", { keys: "mod+j", when: ["chat"] }),
    ], [], { platform: "mac" });

    const [conflict] = findConflicts(keymap);
    assert.equal(conflict.kind, "duplicate");
    assert.deepEqual(conflict.ids.sort(), ["a.one", "a.two"]);
  });

  test("two screens binding the same key to their own search box is not one", () => {
    // This is the whole reason scope overlap is computed rather than assumed.
    const keymap = buildKeymap([
      command("notes.find", { keys: "/", when: ["notes"] }),
      command("gallery.find", { keys: "/", when: ["gallery"] }),
    ], [], { platform: "mac" });

    assert.deepEqual(findConflicts(keymap), []);
  });

  test("a global binding does collide with a scoped one, because both can hold", () => {
    const keymap = buildKeymap([
      command("a.anywhere", { keys: "mod+j" }),
      command("a.inchat", { keys: "mod+j", when: ["chat"] }),
    ], [], { platform: "mac" });

    assert.equal(findConflicts(keymap)[0]?.kind, "duplicate");
  });

  test("a sequence that swallows its own prefix is a shadow", () => {
    // Binding "g" alone makes every "g x" unreachable, and nothing about the
    // longer binding looks wrong when you read it.
    const keymap = buildKeymap([
      command("a.short", { keys: "g" }),
      command("a.long", { keys: "g c" }),
    ], [], { platform: "mac" });

    const [conflict] = findConflicts(keymap);
    assert.equal(conflict.kind, "shadow");
    assert.deepEqual(conflict.ids, ["a.short", "a.long"]);
  });

  test("a prefix in another screen is not a shadow", () => {
    const keymap = buildKeymap([
      command("a.short", { keys: "g", when: ["notes"] }),
      command("a.long", { keys: "g c", when: ["gallery"] }),
    ], [], { platform: "mac" });

    assert.deepEqual(findConflicts(keymap), []);
  });

  test("a command does not conflict with itself across its two slots", () => {
    // Reaching one command by the same key in both slots is harmless -- the key
    // runs that command either way. Reporting it would send somebody off to fix
    // "Settings shares this key with Settings".
    const keymap = buildKeymap(COMMANDS, [
      { command_id: "app.openSettings", keymap: "alternate", sequence: "mod+," },
    ], { platform: "mac" });

    assert.deepEqual(findConflicts(keymap), []);
  });

  test("but two different commands on one key still are in conflict", () => {
    const keymap = buildKeymap(COMMANDS, [
      { command_id: "app.openSettings", keymap: "alternate", sequence: "c" },
    ], { platform: "mac" });

    const clash = findConflicts(keymap).find((entry) => entry.kind === "duplicate");
    assert.deepEqual(clash.ids.sort(), ["app.openSettings", "chat.new"]);
  });

  test("an override onto a reserved chord is flagged, a default on one is not", () => {
    const overridden = buildKeymap([command("a.one", { keys: "mod+j" })], [
      { command_id: "a.one", keymap: "primary", sequence: "escape" },
    ], { platform: "mac" });
    assert.equal(findConflicts(overridden)[0]?.kind, "reserved");

    const shipped = buildKeymap([command("a.one", { keys: "escape" })], [], { platform: "mac" });
    assert.deepEqual(findConflicts(shipped), []);
  });

  test("an override onto a browser-owned chord is flagged as unpreventable", () => {
    const keymap = buildKeymap([command("a.one", { keys: "mod+j" })], [
      { command_id: "a.one", keymap: "primary", sequence: "mod+t" },
    ], { platform: "mac" });

    assert.equal(findConflicts(keymap)[0]?.kind, "unpreventable");
  });
});

describe("looking a run of chords up", () => {
  const keymap = buildKeymap([
    command("a.single", { keys: "mod+k" }),
    command("a.seq", { keys: "g c" }),
    command("a.scoped", { keys: "g n", when: ["notes"] }),
    command("a.specific", { keys: "mod+k", when: ["chat"] }),
  ], [], { platform: "mac" });

  test("a complete binding runs", () => {
    const result = matchSequence(keymap, ["g", "c"], new Set());
    assert.equal(result.status, "run");
    assert.equal(result.binding.id, "a.seq");
  });

  test("a prefix waits rather than running or clearing", () => {
    assert.equal(matchSequence(keymap, ["g"], new Set()).status, "pending");
  });

  test("chords that can become nothing report none", () => {
    assert.equal(matchSequence(keymap, ["z"], new Set()).status, "none");
    assert.equal(matchSequence(keymap, ["g", "z"], new Set()).status, "none");
  });

  test("a prefix whose only continuations are out of scope does not wait", () => {
    // Otherwise "g" eats the keystroke on a screen where no "g x" can ever fire,
    // and the user is typing into a buffer that will never resolve.
    const notesOnly = buildKeymap([command("a.scoped", { keys: "g n", when: ["notes"] })], [], {});
    assert.equal(matchSequence(notesOnly, ["g"], new Set(["gallery"])).status, "none");
    assert.equal(matchSequence(notesOnly, ["g"], new Set(["notes"])).status, "pending");
  });

  test("the more specific binding wins where both apply", () => {
    assert.equal(matchSequence(keymap, ["meta+k"], new Set()).binding.id, "a.single");
    assert.equal(matchSequence(keymap, ["meta+k"], new Set(["chat"])).binding.id, "a.specific");
  });

  test("a scoped binding is invisible from another screen", () => {
    assert.equal(matchSequence(keymap, ["g", "n"], new Set()).status, "none");
    assert.equal(matchSequence(keymap, ["g", "n"], new Set(["notes"])).status, "run");
  });
});

describe("the slot names", () => {
  test("are the two values the database stores", () => {
    assert.deepEqual(SLOTS, ["primary", "alternate"]);
  });

  test("there is no mode left to pass in", () => {
    // One keymap. Both keys live. buildKeymap takes a platform and nothing else.
    const keymap = buildKeymap(COMMANDS, [], { platform: "mac" });
    assert.equal(keymap.commandMode, undefined);
    assert.equal(keymap.name, undefined);
  });

  test("a key already taken is reported before anything is written", () => {
    // This is what puts the choice to the user instead of silently saving a
    // keymap where one of the two commands can never be reached.
    const keymap = buildKeymap(COMMANDS, [], { platform: "mac" });

    const taken = wouldCollideWith(keymap, "c", "app.openSettings", ["global"]);
    assert.deepEqual(taken.map((entry) => entry.id), ["chat.new"]);
    assert.equal(taken[0].slot, "alternate");
  });

  test("a command is never reported as colliding with itself", () => {
    const keymap = buildKeymap(COMMANDS, [], { platform: "mac" });
    assert.deepEqual(wouldCollideWith(keymap, "c", "chat.new", ["chat"]), []);
  });

  test("a free key collides with nothing", () => {
    const keymap = buildKeymap(COMMANDS, [], { platform: "mac" });
    assert.deepEqual(wouldCollideWith(keymap, "mod+alt+ctrl+q", "chat.new", ["chat"]), []);
    assert.deepEqual(wouldCollideWith(keymap, "", "chat.new", ["chat"]), []);
  });

  test("a key taken only on another screen is not a collision", () => {
    const keymap = buildKeymap(COMMANDS, [], { platform: "mac" });
    assert.deepEqual(wouldCollideWith(keymap, "/", "chat.new", ["gallery"]).map((e) => e.id),
      ["gallery.focusSearch"]);
  });

  test("scope overlap is symmetric", () => {
    assert.equal(scopesOverlap(["notes"], ["gallery"]), false);
    assert.equal(scopesOverlap(["gallery"], ["notes"]), false);
    assert.equal(scopesOverlap(["global"], ["notes"]), true);
    assert.equal(scopesOverlap(["notes"], ["global"]), true);
  });
});
