/**
 * State that belongs to one profile must not survive into the next one.
 *
 * localStorage is scoped to the origin, not to the profile, so every key holding
 * a row id is a cross-profile leak waiting to happen: the next profile boots
 * pointing at a chat that only ever existed in the previous profile's
 * store. These pin that both profile transitions -- switching out, and signing
 * in through the picker -- drop every such key.
 */
import assert from "node:assert/strict";
import { beforeEach, describe, test } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

const store = new Map();
globalThis.localStorage = {
  getItem: (key) => (store.has(key) ? store.get(key) : null),
  setItem: (key, value) => store.set(key, String(value)),
  removeItem: (key) => store.delete(key),
};
globalThis.window = globalThis;

const { PROFILE_SCOPED_STORAGE_KEYS, clearProfileScopedState } = await import("../src/App.jsx");

describe("profile-scoped storage", () => {
  beforeEach(() => store.clear());

  test("the active chat id is treated as profile-scoped, not global", () => {
    // The regression: a row id was persisted globally and cleared nowhere, so a
    // new profile opened the previous profile's thread and hung on it.
    assert.ok(PROFILE_SCOPED_STORAGE_KEYS.includes("neo-active-chat-id"));
  });

  test("a run is not remembered separately from the chat it happened in", () => {
    // A run is a turn of a chat now, so remembering its id as well would be a
    // second pointer into profile data that the first one already covers -- and
    // a second thing to forget to clear.
    assert.ok(!PROFILE_SCOPED_STORAGE_KEYS.includes("neo-agent-session-id"));
  });

  test("clearing drops every profile-scoped key, not just the chat id", () => {
    for (const key of PROFILE_SCOPED_STORAGE_KEYS) store.set(key, "7");

    clearProfileScopedState();

    assert.deepEqual([...store.keys()], []);
  });

  test("state that is not profile-scoped is left alone", () => {
    store.set("neo-theme", "dark");

    clearProfileScopedState();

    assert.equal(store.get("neo-theme"), "dark");
  });

  test("clearing works when storage is unavailable, as in private mode", () => {
    const real = globalThis.localStorage;
    globalThis.localStorage = {
      removeItem() {
        throw new Error("storage disabled");
      },
    };

    assert.doesNotThrow(clearProfileScopedState);

    globalThis.localStorage = real;
  });
});
