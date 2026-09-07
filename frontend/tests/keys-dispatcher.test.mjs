/**
 * Sequences, counts, scope, and the one line that lets local handlers win.
 *
 * The timeout is a method rather than a timer, so everything here runs without a
 * clock: `expire()` is exactly what the engine calls when 900ms passes. The
 * defaultPrevented case is the load-bearing one -- it is how the composer's
 * Enter, Notes' Tab and Research's mod+Enter keep working without either side
 * being taught about the other.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { MAX_COUNT, createDispatcher } from "../src/keys/dispatcher.js";
import { buildKeymap, findConflicts } from "../src/keys/keymap.js";

const command = (id, extra = {}) => ({ id, title: id, section: "Test", keys: "", ...extra });
const press = (key, flags = {}) => ({
  key, altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, ...flags,
});
const loose = { scopes: new Set(), focusKind: "none" };

const CATALOGUE = [
  command("a.single", { keys: "mod+k" }),
  command("a.seq", { keys: "g c" }),
  command("a.prefixIsAlsoBinding", { keys: "d" }),
  command("a.longer", { keys: "d d" }),
  command("a.scrolls", { keys: "j", repeatable: true }),
  command("a.once", { keys: "x" }),
  command("a.inChat", { keys: "z", when: ["chat"] }),
];

function dispatcher(options = {}) {
  const { commandMode = true, catalogue = CATALOGUE, overrides = [] } = options;
  const keymap = buildKeymap(catalogue, overrides, { platform: "mac", commandMode });
  return createDispatcher({ keymap, commandMode });
}

describe("single chords", () => {
  test("a complete binding runs and asks for the keystroke", () => {
    const result = dispatcher().feed(press("k", { metaKey: true }), loose);
    assert.equal(result.action, "run");
    assert.equal(result.commandId, "a.single");
    assert.equal(result.preventDefault, true);
  });

  test("a chord bound to nothing leaves the keystroke alone", () => {
    const result = dispatcher().feed(press("q"), loose);
    assert.equal(result.action, "none");
    assert.equal(result.preventDefault, false);
  });

  test("an event that carries no key at all is ignored rather than thrown on", () => {
    assert.equal(dispatcher().feed(undefined, loose).action, "none");
    assert.equal(dispatcher().feed({}, loose).action, "none");
  });
});

describe("an event something nearer already handled", () => {
  test("is skipped entirely, whatever it is bound to", () => {
    // The composer calls preventDefault on a bare Enter before this ever runs.
    const result = dispatcher().feed(press("k", { metaKey: true, defaultPrevented: true }), loose);
    assert.equal(result.action, "none");
  });

  test("does not disturb a sequence already in progress", () => {
    const keys = dispatcher();
    keys.feed(press("g"), loose);
    keys.feed(press("c", { defaultPrevented: true }), loose);
    assert.deepEqual(keys.pending, ["g"], "the buffer is left exactly as it was");
  });
});

describe("sequences", () => {
  test("a prefix waits without running anything", () => {
    const keys = dispatcher();
    const result = keys.feed(press("g"), loose);
    assert.equal(result.action, "pending");
    assert.equal(result.commandId, null);
    assert.deepEqual(keys.pending, ["g"]);
  });

  test("a prefix holds the keystroke back so find-as-you-type does not open", () => {
    assert.equal(dispatcher().feed(press("g"), loose).preventDefault, true);
  });

  test("completing the sequence runs it and empties the buffer", () => {
    const keys = dispatcher();
    keys.feed(press("g"), loose);
    const result = keys.feed(press("c"), loose);
    assert.equal(result.commandId, "a.seq");
    assert.equal(result.sequence, "g c");
    assert.deepEqual(keys.pending, []);
  });

  test("a dead end clears the buffer rather than leaving it to rot", () => {
    const keys = dispatcher();
    keys.feed(press("g"), loose);
    const result = keys.feed(press("q"), loose);
    assert.equal(result.action, "none");
    assert.deepEqual(keys.pending, []);
  });

  test("the buffer is usable again straight after a dead end", () => {
    const keys = dispatcher();
    keys.feed(press("g"), loose);
    keys.feed(press("q"), loose);
    keys.feed(press("g"), loose);
    assert.equal(keys.feed(press("c"), loose).commandId, "a.seq");
  });
});

describe("the timeout", () => {
  test("drops a prefix that was only ever a prefix", () => {
    const keys = dispatcher();
    keys.feed(press("g"), loose);
    const result = keys.expire(loose);
    assert.equal(result.action, "none");
    assert.deepEqual(keys.pending, []);
  });

  test("expiring with nothing pending does nothing", () => {
    assert.equal(dispatcher().expire(loose).action, "none");
  });
});

describe("a chord that is also the start of a longer one", () => {
  test("runs on its own straight away, rather than pausing to see what follows", () => {
    // The alternative is a visible delay on every such key. This is the trade the
    // shadow conflict exists to warn about.
    const keys = dispatcher();
    const result = keys.feed(press("d"), loose);
    assert.equal(result.action, "run");
    assert.equal(result.commandId, "a.prefixIsAlsoBinding");
  });

  test("makes the longer binding unreachable, which is what a shadow means", () => {
    const keys = dispatcher();
    keys.feed(press("d"), loose);
    assert.notEqual(keys.feed(press("d"), loose).commandId, "a.longer");
  });

  test("is reported, so the settings screen can say so while it is being recorded", () => {
    const keymap = buildKeymap(CATALOGUE, [], { platform: "mac", commandMode: true });
    const shadow = findConflicts(keymap).find((entry) => entry.kind === "shadow");
    assert.deepEqual(shadow?.ids, ["a.prefixIsAlsoBinding", "a.longer"]);
  });
});

describe("counts", () => {
  test("a repeat is carried to a command that accepts one", () => {
    const keys = dispatcher();
    assert.equal(keys.feed(press("3"), loose).action, "pending");
    assert.equal(keys.feed(press("j"), loose).count, 3);
  });

  test("no repeat means once", () => {
    assert.equal(dispatcher().feed(press("j"), loose).count, 1);
  });

  test("a command that cannot repeat is run once whatever was typed", () => {
    const keys = dispatcher();
    keys.feed(press("5"), loose);
    assert.equal(keys.feed(press("x"), loose).count, 1);
  });

  test("an absurd repeat is clamped rather than obeyed", () => {
    const keys = dispatcher();
    for (const digit of "9999") keys.feed(press(digit), loose);
    assert.equal(keys.feed(press("j"), loose).count, MAX_COUNT);
  });

  test("a leading zero is a key, not a count", () => {
    // Left free so "0" can be bound to something.
    const keys = dispatcher();
    assert.equal(keys.feed(press("0"), loose).action, "none");
    assert.equal(keys.count, "");
  });

  test("zero after a digit extends the count", () => {
    const keys = dispatcher();
    keys.feed(press("1"), loose);
    keys.feed(press("0"), loose);
    assert.equal(keys.feed(press("j"), loose).count, 10);
  });

  test("with Command mode off a digit is never a count", () => {
    const keys = dispatcher({ commandMode: false });
    assert.equal(keys.feed(press("3"), loose).action, "none");
    assert.equal(keys.count, "");
  });

  test("a count is spent by the command it preceded", () => {
    const keys = dispatcher();
    keys.feed(press("3"), loose);
    keys.feed(press("j"), loose);
    assert.equal(keys.feed(press("j"), loose).count, 1);
  });
});

describe("scope", () => {
  test("a binding out of scope does not run", () => {
    assert.equal(dispatcher().feed(press("z"), loose).action, "none");
  });

  test("the same key runs once its screen is showing", () => {
    const context = { scopes: new Set(["chat"]), focusKind: "none" };
    assert.equal(dispatcher().feed(press("z"), context).commandId, "a.inChat");
  });

  test("the more specific of two bindings wins", () => {
    const keys = dispatcher({
      catalogue: [command("a.anywhere", { keys: "w" }), command("a.here", { keys: "w", when: ["chat"] })],
    });
    assert.equal(keys.feed(press("w"), { scopes: new Set(["chat"]), focusKind: "none" }).commandId, "a.here");
  });
});

describe("overrides reach the dispatcher intact", () => {
  test("a Command mode binding is added, and the always-on one still works", () => {
    // The two keymaps are independent slots. Binding a Command mode key must not
    // quietly retire the chord somebody's fingers already know.
    const keys = dispatcher({
      overrides: [{ command_id: "a.once", keymap: "command", sequence: "w" }],
    });
    assert.equal(keys.feed(press("w"), loose).commandId, "a.once");
    assert.equal(keys.feed(press("x"), loose).commandId, "a.once");
  });

  test("rebinding the always-on key does replace it", () => {
    const keys = dispatcher({
      overrides: [{ command_id: "a.once", keymap: "standard", sequence: "w" }],
    });
    assert.equal(keys.feed(press("w"), loose).commandId, "a.once");
    assert.equal(keys.feed(press("x"), loose).action, "none");
  });

  test("a command unbound in both keymaps answers to nothing", () => {
    const keys = dispatcher({
      overrides: [
        { command_id: "a.once", keymap: "standard", sequence: "" },
        { command_id: "a.once", keymap: "command", sequence: "" },
      ],
    });
    assert.equal(keys.feed(press("x"), loose).action, "none");
  });
});

describe("reset", () => {
  test("throws away whatever was half-typed", () => {
    const keys = dispatcher();
    keys.feed(press("3"), loose);
    keys.feed(press("g"), loose);
    keys.reset();
    assert.deepEqual(keys.pending, []);
    assert.equal(keys.count, "");
  });

  test("the buffer it hands out cannot be used to change it", () => {
    const keys = dispatcher();
    keys.feed(press("g"), loose);
    keys.pending.push("nonsense");
    assert.deepEqual(keys.pending, ["g"]);
  });
});
