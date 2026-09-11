/**
 * Saving a whole-value preference while the user keeps clicking.
 *
 * This is the regression the settings panel shipped with: every toggle was
 * disabled until the write returned, so seven controls could only be used one
 * at a time and the second click of a pair landed in the round trip and was
 * swallowed. The fix is not "remove the disabled attribute" -- that alone gives
 * you four writes racing and the slowest reply repainting last. It is that the
 * writes coalesce, which these pin.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { createSetWriter, sameSet } from "../src/setWriter.js";

/** A send that resolves when the test says so, and records what it was asked. */
function deferredSend() {
  const calls = [];
  const send = (value) => {
    let settle;
    const promise = new Promise((resolve, reject) => {
      settle = { ok: () => resolve(value), fail: () => reject(new Error("nope")) };
    });
    calls.push({ value, ...settle });
    return promise;
  };
  return { send, calls };
}

/** Let every already-resolved promise run its continuations. */
const settle = () => new Promise((resolve) => setImmediate(resolve));

function writer(overrides = {}) {
  const painted = [];
  const errors = [];
  const { send, calls } = deferredSend();
  const instance = createSetWriter({
    send,
    onState: (value) => painted.push(value),
    onError: () => errors.push("failed"),
    initial: [],
    ...overrides,
  });
  return { instance, calls, painted, errors };
}

describe("comparing sets", () => {
  test("order is not a difference", () => {
    // The server normalises to catalogue order, so a value that came back from
    // it must not read as a change from the one that was sent.
    assert.ok(sameSet(["a", "b"], ["b", "a"]));
    assert.ok(sameSet([], []));
    assert.ok(!sameSet(["a"], ["a", "b"]));
    assert.ok(!sameSet(["a"], ["b"]));
  });
});

describe("clicking faster than the server replies", () => {
  test("every click paints at once, however many are in flight", async () => {
    const { instance, painted } = writer();

    instance.set(["a"]);
    instance.set(["a", "b"]);
    instance.set(["a", "b", "c"]);

    assert.deepEqual(painted, [["a"], ["a", "b"], ["a", "b", "c"]]);
  });

  test("only one write is ever in flight", async () => {
    const { instance, calls } = writer();

    instance.set(["a"]);
    instance.set(["a", "b"]);
    instance.set(["a", "b", "c"]);
    await settle();

    assert.equal(calls.length, 1, "three clicks put three writes on the wire");
    assert.deepEqual(calls[0].value, ["a"]);
  });

  test("the waypoints are never sent -- the newest value goes next", async () => {
    const { instance, calls } = writer();

    instance.set(["a"]);
    await settle();
    instance.set(["a", "b"]);
    instance.set(["a", "b", "c"]);
    calls[0].ok();
    await settle();

    assert.equal(calls.length, 2);
    assert.deepEqual(calls[1].value, ["a", "b", "c"], "an intermediate value was sent");
  });

  test("the server ends on the last thing asked for", async () => {
    const { instance, calls } = writer();

    instance.set(["a"]);
    await settle();
    instance.set(["b"]);
    calls[0].ok();
    await settle();
    calls[1].ok();
    await settle();

    assert.equal(calls.length, 2);
    assert.deepEqual(calls.at(-1).value, ["b"]);
    assert.ok(!instance.busy(), "the loop never finished");
    assert.deepEqual(instance.desired(), ["b"]);
  });

  test("toggling everything on and back off settles, and sends no more", async () => {
    const { instance, calls } = writer();

    instance.set(["a"]);
    await settle();
    instance.set([]);
    calls[0].ok();
    await settle();
    calls[1].ok();
    await settle();

    // Two writes, not one: the server was told ["a"] and has to be told it is
    // not that any more. What must not happen is a third.
    assert.equal(calls.length, 2);
    assert.deepEqual(instance.desired(), []);
  });
});

describe("a reply that is already stale", () => {
  test("does not repaint over a click made while it was in flight", async () => {
    const { instance, calls, painted } = writer();

    instance.set(["a"]);
    await settle();
    instance.set(["a", "b"]);
    painted.length = 0;

    calls[0].ok();
    await settle();

    assert.deepEqual(painted, [], "the screen was dragged back to an older state");
  });

  test("a reply nothing has moved past is taken as the truth", async () => {
    // The server normalises order and drops duplicates, so its answer is worth
    // adopting rather than assuming the write landed exactly as sent.
    const { instance, painted } = writer({ send: async () => ["normalised"] });

    instance.set(["b", "a"]);
    await settle();

    assert.deepEqual(painted.at(-1), ["normalised"]);
    assert.deepEqual(instance.desired(), ["normalised"]);
  });
});

describe("when a write fails", () => {
  test("the screen goes back to the last thing the server agreed to", async () => {
    const { instance, calls, painted, errors } = writer();

    instance.set(["a"]);
    await settle();
    calls[0].ok();
    await settle();

    instance.set(["a", "b"]);
    await settle();
    calls[1].fail();
    await settle();

    assert.deepEqual(painted.at(-1), ["a"]);
    assert.deepEqual(errors, ["failed"]);
  });

  test("clicks made while it was in flight go back too", async () => {
    // They were never sent and never stored, so leaving them on screen would be
    // the panel claiming something it does not know.
    const { instance, calls, painted } = writer();

    instance.set(["a"]);
    await settle();
    calls[0].ok();
    await settle();

    instance.set(["a", "b"]);
    await settle();
    instance.set(["a", "b", "c"]);
    calls[1].fail();
    await settle();

    assert.deepEqual(painted.at(-1), ["a"]);
    assert.deepEqual(instance.desired(), ["a"]);
  });

  test("the writer keeps working afterwards", async () => {
    const { instance, calls } = writer();

    instance.set(["a"]);
    await settle();
    calls[0].fail();
    await settle();

    assert.ok(!instance.busy(), "a failure left the loop latched");

    instance.set(["b"]);
    await settle();
    assert.equal(calls.length, 2, "the next click was not sent");
  });
});

describe("a value from somewhere else", () => {
  test("is adopted while idle", async () => {
    // The first load can land while the panel is already open.
    const { instance, calls } = writer();

    instance.adopt(["fromTheServer"]);
    instance.set(["fromTheServer", "a"]);
    await settle();

    assert.deepEqual(calls[0].value, ["fromTheServer", "a"], "the loaded value was lost");
  });

  test("is ignored mid-flight, where this writer holds the newer truth", async () => {
    const { instance, calls } = writer();

    instance.set(["a"]);
    await settle();
    instance.adopt([]);

    assert.deepEqual(instance.desired(), ["a"]);
    calls[0].ok();
    await settle();
    assert.equal(calls.length, 1, "adopting mid-flight started a fight with the server");
  });
});

describe("nothing can switch the writer off", () => {
  /* The bug this replaces, and the one the whole feature was reported as. The
     writer had a `stop()` for the panel to call on unmount. React runs effects
     mount / cleanup / mount under StrictMode, the writer lives in a ref and
     survives that remount, and `stop()` had no counterpart -- so in development
     it was dead before the first click. Every toggle painted optimistically and
     sent nothing, and the entry came back on the next refresh. The server log
     had no POST in it at all. */

  test("no part of this API can leave it unable to save", () => {
    // If a disable ever comes back it needs a matching enable, and the test
    // below needs to cover the order React actually calls them in.
    const { instance } = writer();
    assert.deepEqual(
      Object.keys(instance).sort(),
      ["adopt", "busy", "desired", "set"],
      "a lifecycle hook was added; make sure a remount cannot leave it off",
    );
  });

  test("the panel's effects running twice does not stop it saving", async () => {
    // StrictMode's mount / cleanup / mount, as the panel performs it: the only
    // thing it does to the writer is adopt the prop it already holds.
    const { instance, calls } = writer();

    instance.adopt([]);
    instance.adopt([]);

    instance.set(["memory"]);
    await settle();

    assert.equal(calls.length, 1, "the toggle sent nothing");
    assert.deepEqual(calls[0].value, ["memory"]);
  });

  test("a toggle made just before the dialog closes still lands", async () => {
    // The write was asked for. Whether the dialog that asked is still open is
    // not the server's business, and `onState` reaches the component that owns
    // the sidebar, which outlives the panel.
    const { instance, calls, painted } = writer();

    instance.set(["a"]);
    await settle();
    assert.equal(calls.length, 1, "closing the dialog swallowed the write");

    calls[0].ok();
    await settle();
    assert.deepEqual(painted.at(-1), ["a"]);
  });
});
