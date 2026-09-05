/**
 * The sidebar's Archived section and the row action that fills it.
 *
 * Rendered for real rather than asserted on props, because the things that go
 * wrong here are about what is on screen: an Archived heading that appears with
 * nothing behind it, an Archive action offered on a pinned chat it would
 * contradict, or an archived chat whose only way back is missing from its menu.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { Sidebar } from "../src/App.jsx";

function chat(id, title, extra = {}) {
  return { id, title, project_id: null, archived: false, pinned: false, ...extra };
}

function render(overrides = {}) {
  const props = {
    sidebar: { projects: [], chats: [], archived_count: 0, chat_limit: 10 },
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

describe("the Archived section", () => {
  test("stays out of the way entirely when nothing is archived", () => {
    const markup = render({
      sidebar: { projects: [], chats: [chat(1, "Only chat")], archived_count: 0, chat_limit: 10 },
    });

    assert.ok(!markup.includes("ARCHIVED"));
    assert.ok(markup.includes("Only chat"));
  });

  test("names how many are in there, and starts shut", () => {
    const markup = render({
      sidebar: { projects: [], chats: [chat(1, "Recent")], archived_count: 7, chat_limit: 10 },
    });

    assert.ok(markup.includes("ARCHIVED"));
    assert.ok(markup.includes(">7<"));
    // Closed on arrival: the list behind it is a second request, and the whole
    // point of the count is that the heading is honest without making it.
    assert.ok(markup.includes('aria-expanded="false"'));
  });
});

describe("the row's Archive action", () => {
  test("is offered on an ordinary chat", () => {
    const markup = render({
      sidebar: { projects: [], chats: [chat(1, "Ordinary")], archived_count: 0, chat_limit: 10 },
    });

    assert.ok(markup.includes(">Archive<"));
    assert.ok(markup.includes(">Pin chat<"));
  });

  test("is withheld from a pinned chat, which the cap already exempts", () => {
    const markup = render({
      sidebar: {
        projects: [],
        chats: [chat(1, "Held", { pinned: true })],
        archived_count: 0,
        chat_limit: 10,
      },
    });

    assert.ok(markup.includes(">Unpin chat<"));
    assert.ok(!markup.includes(">Archive<"));
  });

  test("is absent where no handler was given, rather than rendering a dead button", () => {
    const markup = render({
      sidebar: { projects: [], chats: [chat(1, "Ordinary")], archived_count: 0, chat_limit: 10 },
      onArchiveChat: undefined,
    });

    assert.ok(!markup.includes(">Archive<"));
    assert.ok(markup.includes(">Rename<"));
  });
});
