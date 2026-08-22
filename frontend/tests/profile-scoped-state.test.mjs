/**
 * State that belongs to one profile must not survive into the next one.
 *
 * localStorage is scoped to the origin, not to the profile, so every key holding
 * a row id is a cross-profile leak waiting to happen: the next profile boots
 * pointing at a chat or a run that only ever existed in the previous profile's
 * store. These pin that both profile transitions -- switching out, and signing
 * in through the picker -- drop every such key, and that a view handed an id it
 * cannot load stays escapable instead of covering the app forever.
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
const { SessionUnavailable } = await import("../src/AgentSession.jsx");

describe("profile-scoped storage", () => {
  beforeEach(() => store.clear());

  test("the active run id is treated as profile-scoped, not global", () => {
    // The regression: this key was persisted globally and cleared nowhere, so a
    // new profile opened the previous profile's run and hung on it.
    assert.ok(PROFILE_SCOPED_STORAGE_KEYS.includes("neo-agent-session-id"));
    assert.ok(PROFILE_SCOPED_STORAGE_KEYS.includes("neo-active-chat-id"));
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

describe("a run that cannot be opened", () => {
  test("says why instead of claiming it is still loading", () => {
    const html = renderToStaticMarkup(
      createElement(SessionUnavailable, { error: "agent_session_not_found", onClose() {} }),
    );

    assert.ok(html.includes("agent_session_not_found"));
    assert.ok(!html.includes("Loading run"));
  });

  test("offers a way back out, so the view is never a dead end", () => {
    const html = renderToStaticMarkup(
      createElement(SessionUnavailable, { error: "boom", onClose() {} }),
    );

    assert.ok(html.includes("agent-session-back"));
  });
});
