/**
 * The sidebar's chat list: ten rows tall, scrolling, and complete.
 *
 * The three things that can quietly go wrong here are a list that stretches the
 * sidebar until SYSTEM and Settings fall off the bottom, a list that scrolls but
 * holds only part of the history, and a height that stops matching the row it is
 * supposed to be ten of. One is markup, one is the render, one is the stylesheet,
 * so all three are checked.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { Sidebar } from "../src/App.jsx";

const CSS = readFileSync(new URL("../src/index.css", import.meta.url), "utf8");

function chat(id, title, extra = {}) {
  return { id, title, project_id: null, archived: false, pinned: false, ...extra };
}

function render(overrides = {}) {
  const props = {
    sidebar: { projects: [], chats: [], archived_count: 0 },
    activeChatId: null,
    statusFor: () => null,
    selectedProjectId: null,
    showNewProjectForm: false,
    onToggleProjectForm() {},
    onCreateProject() {},
    onNewChat() {},
    onOpenChat() {},
    onDeleteChat() {},
    onRenameChat() {},
    onPinChat() {},
    onArchiveChat() {},
    onDeleteProject() {},
    onOpenSettings() {},
    onOpenChatHome() {},
    onOpenMemory() {},
    onOpenResearch() {},
    onOpenNotes() {},
    onOpenTasks() {},
    onOpenCalendar() {},
    onOpenGallery() {},
    activeView: "chat",
    profile: { username: "ada", avatar_data: null },
    onSwitchProfile() {},
    ...overrides,
  };
  return renderToStaticMarkup(createElement(Sidebar, props));
}

describe("the chat list", () => {
  test("scrolls in a box of its own rather than growing the sidebar", () => {
    const markup = render({
      sidebar: { projects: [], chats: [chat(1, "Only chat")], archived_count: 0 },
    });

    assert.ok(markup.includes('class="sidebar-scroll-list"'));
  });

  test("holds the whole history, however long, not the first screenful", () => {
    const chats = Array.from({ length: 25 }, (_, index) =>
      chat(index + 1, `Chat ${index + 1}`)
    );
    const markup = render({ sidebar: { projects: [], chats, archived_count: 0 } });

    // The twenty-fifth is as present as the first: scrolling is what puts it out
    // of sight, and putting a chat out of sight for good is what Archive is for.
    for (const each of chats) {
      assert.ok(markup.includes(`>${each.title}<`), `${each.title} is missing`);
    }
  });

  test("is empty-stated rather than rendering an empty scroller", () => {
    const markup = render({ sidebar: { projects: [], chats: [], archived_count: 0 } });

    assert.ok(markup.includes("No chats yet."));
    assert.ok(!markup.includes('class="sidebar-scroll-list"'));
  });
});

describe("the row's actions menu", () => {
  test("is positioned against the window, where the scroller cannot clip it", () => {
    // It used to hang off the row, which is fine until the row is inside a box
    // with `overflow`. Flipping it upward is not a fix on its own: a list
    // holding three chats is shorter than the menu, so both ends clip and the
    // only choice is which half of the menu goes missing.
    const block = CSS.slice(
      CSS.indexOf(".row-actions-menu {"),
      CSS.indexOf(".row-actions-menu[hidden]")
    );

    assert.match(block, /position:\s*fixed/);
    assert.doesNotMatch(block, /position:\s*absolute/);
  });

  test("is still on the row when it is closed, so its actions are in the markup", () => {
    const markup = render({
      sidebar: { projects: [], chats: [chat(1, "Only chat")], archived_count: 0 },
    });

    assert.ok(markup.includes('class="row-actions-menu" hidden=""'));
  });
});

describe("the list's height", () => {
  const block = CSS.slice(CSS.indexOf(".sidebar-scroll-list {"));

  test("is ten rows, derived from the row rather than guessed", () => {
    assert.match(block, /--neo-chat-row:\s*32px/);
    assert.match(block, /max-height:\s*calc\(var\(--neo-chat-row\)\s*\*\s*10\)/);
    assert.match(block, /overflow-y:\s*auto/);
  });

  test("refuses to be the thing a short window squeezes", () => {
    // A scrolling flex item's automatic minimum size is zero, so without this
    // the sidebar's column shrinks the list first and ten rows silently becomes
    // four on a laptop. Measured, not theorised: it did exactly that.
    assert.match(block, /flex:\s*none/);
  });

  test("matches the row it claims to be ten of", () => {
    // `min-height: 30px` plus the 1px margin above and below. If the row changes
    // and this does not, the list stops being ten chats tall.
    assert.match(CSS, /\.project-chat-item,\s*\.chat-item \{[^}]*min-height:\s*30px/);
    assert.match(CSS, /\.chat-item \{ margin: 1px 8px;/);
  });
});
