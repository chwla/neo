/**
 * Smart folder rules in the browser must agree with the server's
 * (app/services/notes/smart.py, tests/test_notes_smart_folders.py): the same
 * cases, the same answers. They decide whether a note joins or leaves an open
 * smart folder the moment it is pinned, moved or tagged.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { defaultRule, matchesRules, ruleIsComplete } from "../src/notes/smartRules.js";

const NOW = new Date("2026-10-15T12:00:00Z");
const note = (fields = {}) => ({
  tags: [],
  created_at: "2026-10-15T09:00:00Z",
  updated_at: "2026-10-15T09:00:00Z",
  checklist_total: 0,
  checklist_done: 0,
  has_images: false,
  has_files: false,
  has_sketches: false,
  folder_id: null,
  pinned: false,
  content_format: "rich",
  ...fields,
});
const one = (rule, fields, options = {}) => matchesRules({ match: "all", rules: [rule] }, note(fields), { now: NOW, tzOffset: 0, ...options });

describe("smart folder rules", () => {
  test("tags", () => {
    assert.equal(one({ field: "tags", op: "includes_any", value: ["home", "#Work"] }, { tags: ["work"] }), true);
    assert.equal(one({ field: "tags", op: "includes_all", value: ["work", "home"] }, { tags: ["work"] }), false);
    assert.equal(one({ field: "tags", op: "includes_all", value: ["work", "home"] }, { tags: ["work", "home"] }), true);
    assert.equal(one({ field: "tags", op: "excludes", value: ["home"] }, { tags: ["work"] }), true);
    assert.equal(one({ field: "tags", op: "has_any" }, {}), false);
    assert.equal(one({ field: "tags", op: "has_none" }, {}), true);
  });

  test("dates, in the viewer's own days", () => {
    assert.equal(one({ field: "edited", op: "today" }, {}), true);
    assert.equal(one({ field: "edited", op: "yesterday" }, { updated_at: "2026-10-14T12:00:00Z" }), true);
    assert.equal(one({ field: "created", op: "last_days", value: 30 }, { created_at: "2026-09-05T12:00:00Z" }), false);
    assert.equal(one({ field: "created", op: "before", value: "2026-10-05" }, { created_at: "2026-09-05T12:00:00Z" }), true);
    assert.equal(one({ field: "created", op: "after", value: "2026-10-05" }, { created_at: "2026-10-05T12:00:00Z" }), false, "after a day means from the next day");
    // 23:30 UTC yesterday: today at UTC+2, yesterday at UTC-2.
    const late = { updated_at: "2026-10-14T23:30:00Z" };
    assert.equal(one({ field: "edited", op: "today" }, late, { tzOffset: 0 }), false);
    assert.equal(one({ field: "edited", op: "today" }, late, { tzOffset: 120 }), true);
    assert.equal(one({ field: "edited", op: "today" }, late, { tzOffset: -120 }), false);
  });

  test("checklists, attachments, folders, pinned and locked", () => {
    assert.equal(one({ field: "checklists", op: "has_unchecked" }, { checklist_total: 2, checklist_done: 1 }), true);
    assert.equal(one({ field: "checklists", op: "all_checked" }, { checklist_total: 2, checklist_done: 2 }), true);
    assert.equal(one({ field: "checklists", op: "none" }, { checklist_total: 1 }), false);
    assert.equal(one({ field: "attachments", op: "any" }, { has_sketches: true }), true);
    assert.equal(one({ field: "attachments", op: "none" }, { has_files: true }), false);
    assert.equal(one({ field: "folder", op: "in", value: ["default"] }, {}), true);
    assert.equal(one({ field: "folder", op: "not_in", value: ["f1"] }, { folder_id: "f1" }), false);
    assert.equal(one({ field: "pinned", op: "is", value: true }, { pinned: true }), true);
    assert.equal(one({ field: "locked", op: "is", value: true }, { content_format: "encrypted" }), true);
    assert.equal(one({ field: "locked", op: "is", value: false }, { content_format: "encrypted" }), false);
  });

  test("all and any", () => {
    const rules = [{ field: "tags", op: "includes_any", value: ["work"] }, { field: "checklists", op: "has" }];
    const tagOnly = note({ tags: ["work"] });
    assert.equal(matchesRules({ match: "all", rules }, tagOnly, { now: NOW, tzOffset: 0 }), false);
    assert.equal(matchesRules({ match: "any", rules }, tagOnly, { now: NOW, tzOffset: 0 }), true);
  });

  test("Recently Deleted and archived notes are never in one", () => {
    const rule = { field: "tags", op: "has_none" };
    assert.equal(one(rule, { deleted: true }), false);
    assert.equal(one(rule, { archived: true }), false);
  });

  test("new rules start complete where they can, and incomplete ones are caught", () => {
    assert.equal(ruleIsComplete(defaultRule("edited", "last_days")), true);
    assert.equal(ruleIsComplete(defaultRule("pinned")), true);
    assert.equal(ruleIsComplete(defaultRule("tags", "includes_any")), false, "a tag must be chosen");
    assert.equal(ruleIsComplete({ field: "created", op: "before", value: "yesterday" }), false);
  });
});
