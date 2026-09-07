/**
 * The listener, the registry it dispatches into, and the two ways it stands down.
 *
 * The suite has no DOM, so window is faked well enough to hold listeners and
 * hand them synthetic events. That is enough to reach the parts that actually go
 * wrong: a command firing while a dialog is open, a command firing while a key is
 * being recorded in the settings screen, and a handler that declines having its
 * keystroke eaten anyway.
 *
 * modalStack is the real one here rather than a stub, because "the engine defers
 * to an open dialog" is a claim about those two modules together.
 */
import assert from "node:assert/strict";
import { beforeEach, describe, test } from "node:test";

const listeners = new Map();

globalThis.window = {
  addEventListener(type, handler) {
    const existing = listeners.get(type);
    if (existing) existing.push(handler);
    else listeners.set(type, [handler]);
  },
  removeEventListener(type, handler) {
    const existing = listeners.get(type) ?? [];
    const index = existing.indexOf(handler);
    if (index >= 0) existing.splice(index, 1);
  },
};
// Node ships its own read-only navigator, so detectPlatform needs it replaced
// rather than assigned over.
Object.defineProperty(globalThis, "navigator", {
  value: { platform: "MacIntel" },
  configurable: true,
  writable: true,
});

const { buildKeymap } = await import("../src/keys/keymap.js");
const { registerModal, resetModalStack } = await import("../src/modalStack.js");
const {
  armEngine, detectPlatform, isEngineArmed, isEngineSuspended,
  onPendingChange, resetEngine, resumeEngine, suspendEngine,
} = await import("../src/keys/engine.js");
const {
  hasHandler, registerCommandHandlers, registeredCommandIds, resetCommandRegistry, runCommand,
} = await import("../src/keys/registry.js");

const command = (id, extra = {}) => ({ id, title: id, section: "Test", keys: "", ...extra });

const CATALOGUE = [
  command("a.single", { keys: "mod+k" }),
  command("a.seq", { keys: "g c" }),
  command("a.bare", { keys: "j" }),
];

/** Sends a keydown to whatever the engine attached, as the browser would. */
function fire(key, flags = {}) {
  const event = {
    key,
    altKey: false, ctrlKey: false, metaKey: false, shiftKey: false,
    defaultPrevented: false,
    target: flags.target ?? { tagName: "DIV" },
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() { this.propagationStopped = true; },
    ...flags,
  };
  for (const handler of [...(listeners.get("keydown") ?? [])]) handler(event);
  return event;
}

function arm(options = {}) {
  const keymap = buildKeymap(CATALOGUE, [], { platform: "mac" });
  return armEngine({ getKeymap: () => keymap, getContext: () => ({ scopes: new Set() }), ...options });
}

beforeEach(() => {
  listeners.clear();
  resetEngine();
  resetCommandRegistry();
  resetModalStack();
});

describe("the registry", () => {
  test("runs the handler registered for an id", () => {
    let ran = 0;
    registerCommandHandlers({ "a.single": () => { ran += 1; } });
    assert.equal(runCommand("a.single"), true);
    assert.equal(ran, 1);
  });

  test("an id nobody claimed is a no-op, not a throw", () => {
    // The catalogue names commands whose screen is not mounted, all the time.
    assert.doesNotThrow(() => runCommand("a.nothing"));
    assert.equal(runCommand("a.nothing"), false);
  });

  test("a handler that declines reports it, so the keystroke can be left alone", () => {
    registerCommandHandlers({ "a.single": () => false });
    assert.equal(runCommand("a.single"), false);
  });

  test("returning nothing is not declining", () => {
    registerCommandHandlers({ "a.single": () => {} });
    assert.equal(runCommand("a.single"), true);
  });

  test("the disposer is safe to call twice", () => {
    const release = registerCommandHandlers({ "a.single": () => {} });
    release();
    assert.doesNotThrow(release);
    assert.equal(hasHandler("a.single"), false);
  });

  test("the most recent registration wins, and its removal restores the earlier one", () => {
    // Which is what a remount looks like from here.
    const ran = [];
    registerCommandHandlers({ "a.single": () => ran.push("first") });
    const release = registerCommandHandlers({ "a.single": () => ran.push("second") });

    runCommand("a.single");
    release();
    runCommand("a.single");

    assert.deepEqual(ran, ["second", "first"]);
  });

  test("unmounting removes its own entry, not whatever is on top", () => {
    const ran = [];
    const releaseFirst = registerCommandHandlers({ "a.single": () => ran.push("first") });
    registerCommandHandlers({ "a.single": () => ran.push("second") });

    releaseFirst();
    runCommand("a.single");

    assert.deepEqual(ran, ["second"]);
  });

  test("non-functions are ignored rather than registered", () => {
    registerCommandHandlers({ "a.single": undefined, "a.seq": null, "a.bare": 7 });
    assert.deepEqual(registeredCommandIds(), []);
    assert.doesNotThrow(() => registerCommandHandlers(undefined));
  });
});

describe("arming and disarming", () => {
  test("attaches one listener and reports itself armed", () => {
    const disarm = arm();
    assert.equal(isEngineArmed(), true);
    assert.equal(listeners.get("keydown")?.length, 1);
    disarm();
    assert.equal(listeners.get("keydown")?.length, 0);
  });

  test("the disposer is safe to call twice", () => {
    const disarm = arm();
    disarm();
    assert.doesNotThrow(disarm);
  });

  test("a disarmed engine runs nothing", () => {
    let ran = 0;
    registerCommandHandlers({ "a.single": () => { ran += 1; } });
    const disarm = arm();
    disarm();
    fire("k", { metaKey: true });
    assert.equal(ran, 0);
  });
});

describe("dispatching", () => {
  test("a bound chord runs its command and takes the keystroke", () => {
    let ran = 0;
    registerCommandHandlers({ "a.single": () => { ran += 1; } });
    arm();

    const event = fire("k", { metaKey: true });

    assert.equal(ran, 1);
    assert.equal(event.defaultPrevented, true);
    assert.equal(event.propagationStopped, true);
  });

  test("a handler that declines leaves the keystroke to the browser", () => {
    // "Stop generating" with nothing generating must not swallow the key.
    registerCommandHandlers({ "a.single": () => false });
    arm();

    const event = fire("k", { metaKey: true });

    assert.equal(event.defaultPrevented, false);
  });

  test("a command with no handler leaves the keystroke alone too", () => {
    arm();
    assert.equal(fire("k", { metaKey: true }).defaultPrevented, false);
  });

  test("an event something nearer already handled is skipped", () => {
    let ran = 0;
    registerCommandHandlers({ "a.single": () => { ran += 1; } });
    arm();

    fire("k", { metaKey: true, defaultPrevented: true });

    assert.equal(ran, 0);
  });

  test("a bare key does not act while a text field has focus", () => {
    let ran = 0;
    registerCommandHandlers({ "a.bare": () => { ran += 1; } });
    arm();

    fire("j", { target: { tagName: "TEXTAREA" } });

    assert.equal(ran, 0);
  });
});

describe("while a dialog is open", () => {
  test("no command runs, however it is bound", () => {
    let ran = 0;
    registerCommandHandlers({ "a.single": () => { ran += 1; }, "a.bare": () => { ran += 1; } });
    arm();
    registerModal(() => {});

    fire("k", { metaKey: true });
    fire("j");

    assert.equal(ran, 0, "the dialog owns the keyboard until it closes");
  });

  test("closing the dialog hands the keyboard back", () => {
    let ran = 0;
    registerCommandHandlers({ "a.single": () => { ran += 1; } });
    arm();
    const close = registerModal(() => {});

    fire("k", { metaKey: true });
    close();
    fire("k", { metaKey: true });

    assert.equal(ran, 1);
  });

  test("escape is left entirely to the dialog stack", () => {
    // Two listeners both acting on Escape is how one keypress closes two things.
    let closed = 0;
    arm();
    registerModal(() => { closed += 1; });

    fire("Escape", { target: { tagName: "TEXTAREA", blur() { this.blurred = true; } } });

    assert.equal(closed, 1, "the dialog closed exactly once");
  });
});

describe("escape, when no dialog is open", () => {
  test("blurs a text field, which is how bare keys become live again", () => {
    arm();
    const target = { tagName: "TEXTAREA", blurred: false, blur() { this.blurred = true; } };

    const event = fire("Escape", { target });

    assert.equal(target.blurred, true);
    assert.equal(event.defaultPrevented, true);
  });

  test("does nothing when nothing has focus", () => {
    arm();
    assert.equal(fire("Escape").defaultPrevented, false);
  });

  test("abandons a half-typed sequence", () => {
    let ran = 0;
    registerCommandHandlers({ "a.seq": () => { ran += 1; } });
    arm();

    fire("g");
    fire("Escape");
    fire("c");

    assert.equal(ran, 0);
  });
});

describe("sequences over the wire", () => {
  test("a prefix holds the keystroke back and announces what is pending", () => {
    const seen = [];
    onPendingChange((chords) => seen.push(chords));
    arm();

    const event = fire("g");

    assert.equal(event.defaultPrevented, true);
    assert.deepEqual(seen.at(-1), ["g"]);
  });

  test("completing the sequence runs it and clears the indicator", () => {
    let ran = 0;
    const seen = [];
    registerCommandHandlers({ "a.seq": () => { ran += 1; } });
    onPendingChange((chords) => seen.push(chords));
    arm();

    fire("g");
    fire("c");

    assert.equal(ran, 1);
    assert.deepEqual(seen.at(-1), []);
  });
});

describe("suspending, which is what the settings screen needs", () => {
  test("stops dispatch without detaching the listener", () => {
    // Recording "g c" as a new binding must not navigate you mid-recording.
    let ran = 0;
    registerCommandHandlers({ "a.single": () => { ran += 1; } });
    arm();

    suspendEngine();
    fire("k", { metaKey: true });

    assert.equal(ran, 0);
    assert.equal(isEngineSuspended(), true);
    assert.equal(isEngineArmed(), true, "still attached, just not acting");
  });

  test("resuming restores it", () => {
    let ran = 0;
    registerCommandHandlers({ "a.single": () => { ran += 1; } });
    arm();

    suspendEngine();
    fire("k", { metaKey: true });
    resumeEngine();
    fire("k", { metaKey: true });

    assert.equal(ran, 1);
  });
});

describe("which modifier mod means", () => {
  test("is read off the platform", () => {
    assert.equal(detectPlatform(), "mac");
  });
});
