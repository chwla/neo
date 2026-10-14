/**
 * Which chat goes on screen, and sidebar edits made before the server agrees.
 *
 * The commit rule is what keeps a slow response from putting the wrong chat on
 * screen: the newest click owns the view, a refresh of the chat being left can
 * never pull the view back, and older data never replaces newer. The sidebar
 * patch is the optimistic half of rename, pin and archive.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { patchSidebarChat, shouldCommitLoad } from "../src/chatView.js";

const view = (over = {}) => ({ navSeq: 0, target: null, committed: 0, ...over });

describe("which load may go on screen", () => {
  test("the newest navigation commits; an older one never does", () => {
    // Clicked A (load 1) then B (load 2).
    const state = view({ navSeq: 2, target: "B" });
    assert.equal(shouldCommitLoad(state, { seq: 1, navigate: true, chatId: "A" }), false);
    assert.equal(shouldCommitLoad(state, { seq: 2, navigate: true, chatId: "B" }), true);
  });

  test("a slow first click on the same chat still loses to the second", () => {
    const state = view({ navSeq: 3, target: "A" });
    assert.equal(shouldCommitLoad(state, { seq: 1, navigate: true, chatId: "A" }), false);
    assert.equal(shouldCommitLoad(state, { seq: 3, navigate: true, chatId: "A" }), true);
  });

  test("a refresh of the chat being left cannot pull the view back", () => {
    // A's turn ended (refresh, load 1) while the person clicked B (load 2).
    const state = view({ navSeq: 2, target: "B" });
    assert.equal(shouldCommitLoad(state, { seq: 1, navigate: false, chatId: "A" }), false);
    assert.equal(shouldCommitLoad(state, { seq: 3, navigate: false, chatId: "A" }), false, "even if it is newer");
  });

  test("a refresh of the chat on screen commits, but never older over newer", () => {
    const state = view({ navSeq: 1, target: "A", committed: 4 });
    assert.equal(shouldCommitLoad(state, { seq: 5, navigate: false, chatId: "A" }), true);
    assert.equal(shouldCommitLoad(state, { seq: 3, navigate: false, chatId: "A" }), false);
  });

  test("while a new chat is being made, no load lands", () => {
    const making = Symbol("new chat");
    const state = view({ navSeq: 6, target: making, committed: 5 });
    assert.equal(shouldCommitLoad(state, { seq: 7, navigate: false, chatId: "A" }), false);
  });

  test("before anything is on screen, the first load is taken", () => {
    assert.equal(shouldCommitLoad(view(), { seq: 1, navigate: false, chatId: "A" }), true);
  });
});

describe("optimistic sidebar edits", () => {
  const sidebar = {
    archived_count: 2,
    chats: [{ id: 1, title: "One", pinned: false }, { id: 2, title: "Two", pinned: false }],
    projects: [{ id: 10, name: "P", chats: [{ id: 3, title: "Three", pinned: false }] }],
  };

  test("renames and pins in place, in loose and project lists alike", () => {
    const renamed = patchSidebarChat(sidebar, 2, { title: "Second" });
    assert.equal(renamed.chats[1].title, "Second");
    assert.equal(renamed.projects, sidebar.projects, "an untouched list keeps its identity");
    const pinned = patchSidebarChat(sidebar, 3, { pinned: true });
    assert.equal(pinned.projects[0].chats[0].pinned, true);
    assert.equal(pinned.chats, sidebar.chats);
  });

  test("archiving hides the row and counts it once", () => {
    const archived = patchSidebarChat(sidebar, 1, { archived: true });
    assert.deepEqual(archived.chats.map((chat) => chat.id), [2]);
    assert.equal(archived.archived_count, 3);
    const again = patchSidebarChat(archived, 1, { archived: true });
    assert.equal(again, archived, "a payload that already reflects it is not counted twice");
  });

  test("unarchiving and unrelated fields leave the payload alone", () => {
    assert.equal(patchSidebarChat(sidebar, 1, { archived: false }), sidebar);
    assert.equal(patchSidebarChat(sidebar, 1, { effort: "high" }), sidebar);
    assert.equal(patchSidebarChat(sidebar, 1, { title: "One" }), sidebar, "nothing to change, same object");
  });
});
