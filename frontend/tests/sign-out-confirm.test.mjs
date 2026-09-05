/**
 * The confirmation in front of the footer avatar.
 *
 * The button sits beside Settings and is the easiest thing in the sidebar to
 * press by accident, and for a guest the server answers it by deleting the
 * profile directory -- chats, notes and memory with it. So what is pinned here
 * is that the dialog says which of those two things is about to happen, and
 * that the guest wording never softens into the reversible one.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { ConfirmSignOutDialog } from "../src/App.jsx";

function render(overrides = {}) {
  return renderToStaticMarkup(
    createElement(ConfirmSignOutDialog, {
      profile: { username: "ada", is_guest: false },
      workingCount: 0,
      onCancel() {},
      onConfirm() {},
      ...overrides,
    }),
  );
}

describe("signing out of a saved profile", () => {
  test("names the profile and promises nothing is lost", () => {
    const markup = render();

    assert.ok(markup.includes("Log out of ada?"));
    assert.ok(markup.includes("Nothing is deleted"));
    assert.ok(!markup.includes("danger"));
  });

  test("still offers a way out", () => {
    assert.ok(render().includes(">Cancel<"));
  });
});

describe("ending a guest session", () => {
  test("says the data goes with it, and marks the action destructive", () => {
    const markup = render({ profile: { username: "guest", is_guest: true } });

    assert.ok(markup.includes("delete its data?"));
    assert.ok(markup.includes("permanently deletes this one"));
    assert.ok(markup.includes("neo-button danger"));
    assert.ok(markup.includes(">Delete and sign out<"));
    // The reassurance belongs to the saved-profile case only. Showing it here
    // would be a lie about a directory that is about to be removed.
    assert.ok(!markup.includes("Nothing is deleted"));
  });
});

describe("work still in flight", () => {
  test("goes unmentioned when nothing is running", () => {
    assert.ok(!render().includes("still working"));
  });

  test("is counted, and reads as English for one", () => {
    assert.ok(render({ workingCount: 1 }).includes("One chat is"));
    assert.ok(render({ workingCount: 3 }).includes("3 chats are"));
  });

  test("tells a saved profile the replies survive, and a guest that they do not", () => {
    assert.ok(render({ workingCount: 2 }).includes("until you sign back in"));

    const guest = render({
      profile: { username: "guest", is_guest: true },
      workingCount: 2,
    });
    assert.ok(guest.includes("deleted with the profile"));
  });
});
