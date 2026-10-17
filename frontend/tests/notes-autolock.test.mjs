/**
 * Locked notes' keys in memory, and auto-lock.
 *
 * What must hold: 15 minutes without input locks, by the wall clock (time
 * asleep or hidden counts, even if no timer ran); input after the deadline
 * doesn't revive the keys; the steps before locking run while the keys still
 * exist, and the keys go even if a step fails.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { AUTO_LOCK_MS, createKeySession } from "../src/notes/noteKeys.js";

function setup() {
  let clock = 1_000_000;
  const intervals = new Set();
  const timers = {
    setInterval: (fn) => {
      const handle = { fn };
      intervals.add(handle);
      return handle;
    },
    clearInterval: (handle) => intervals.delete(handle),
  };
  const transport = {
    unlock: async (password) => {
      if (password !== "pw") throw Object.assign(new Error("That password does not match this profile."), { status: 401 });
      return { wraps: [{ key_version: 1 }], current_version: 1, unavailable: [] };
    },
    setup: async () => ({ wraps: [{ key_version: 1 }], current_version: 1, unavailable: [] }),
    rewrap: async () => ({ wraps: [{ key_version: 1 }, { key_version: 2 }], current_version: 2, unavailable: [] }),
  };
  const open = async (_password, wraps) => new Map(wraps.map((w) => [w.key_version, { version: w.key_version }]));
  const keys = createKeySession({ transport, open, now: () => clock, timers });
  return {
    keys,
    advance: (ms) => (clock += ms),
    tick: () => [...intervals].forEach((handle) => handle.fn()),
    intervals,
  };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("keys in memory", () => {
  test("the profile password opens them; a wrong one opens nothing", async () => {
    const { keys } = setup();
    await assert.rejects(keys.unlock("nope"), /does not match/);
    assert.equal(keys.isUnlocked(), false);
    await keys.unlock("pw");
    assert.deepEqual(keys.state(), { unlocked: true, currentVersion: 1, versions: [1], unavailable: [] });
    assert.ok(keys.keyFor(1));
  });

  test("15 minutes without input locks, even with no timer having run (asleep, hidden)", async () => {
    const { keys, advance } = setup();
    await keys.unlock("pw");
    advance(AUTO_LOCK_MS - 1000);
    assert.ok(keys.keyFor(1), "not yet");
    advance(1000);
    assert.equal(keys.keyFor(1), null, "the deadline passed while nothing ran");
    await settle();
    assert.equal(keys.isUnlocked(), false);
  });

  test("the clock locks on its own too", async () => {
    const { keys, advance, tick, intervals } = setup();
    await keys.unlock("pw");
    advance(AUTO_LOCK_MS);
    tick();
    await settle();
    assert.equal(keys.isUnlocked(), false);
    assert.equal(intervals.size, 0, "and stops once locked");
  });

  test("input keeps them open; input after the deadline does not bring them back", async () => {
    const { keys, advance } = setup();
    await keys.unlock("pw");
    advance(AUTO_LOCK_MS - 5000);
    keys.touch();
    advance(AUTO_LOCK_MS - 5000);
    assert.ok(keys.keyFor(1), "each input restarts the 15 minutes");
    advance(10_000);
    keys.touch();
    await settle();
    assert.equal(keys.isUnlocked(), false);
  });

  test("before the keys go, the steps run with the keys still there; a failing step doesn't keep them", async () => {
    const { keys, advance } = setup();
    await keys.unlock("pw");
    const seen = [];
    keys.onBeforeLock(async (reason) => {
      seen.push(["read-only", reason, Boolean(keys.keyFor(1))]);
    });
    keys.onBeforeLock(async () => {
      seen.push(["seal", Boolean(keys.keyFor(1))]);
      throw new Error("storage full");
    });
    advance(AUTO_LOCK_MS);
    await keys.lock("idle");
    assert.deepEqual(seen, [["read-only", "idle", true], ["seal", true]]);
    assert.equal(keys.isUnlocked(), false);
    assert.equal(keys.keyFor(1), null);
  });

  test("a step that asks to lock again (or for a key) does not start a second lock", async () => {
    const { keys } = setup();
    await keys.unlock("pw");
    let runs = 0;
    keys.onBeforeLock(() => {
      runs += 1;
      keys.lock();
      keys.touch();
      return keys.keyFor(1);
    });
    await keys.lock();
    assert.equal(runs, 1);
    assert.equal(keys.isUnlocked(), false);
  });

  test("locking twice runs once; disposing (profile switch, sign-out) locks", async () => {
    const { keys } = setup();
    await keys.unlock("pw");
    let runs = 0;
    keys.onBeforeLock(async () => {
      runs += 1;
    });
    await Promise.all([keys.lock(), keys.lock()]);
    assert.equal(runs, 1);
    await keys.unlock("pw");
    await keys.dispose();
    assert.equal(keys.isUnlocked(), false);
    assert.equal(runs, 2);
  });

  test("recovering with an earlier password opens every version", async () => {
    const { keys } = setup();
    await keys.recover("pw", "old");
    assert.deepEqual(keys.state().versions, [1, 2]);
    assert.equal(keys.currentVersion(), 2);
  });
});
