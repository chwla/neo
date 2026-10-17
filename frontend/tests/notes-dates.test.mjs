/**
 * Apple's date sections and row dates, at their boundaries, in local time.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { dateGroup, groupNotes, rowDate } from "../src/notes/noteDates.js";

const now = new Date(2026, 9, 15, 14, 30); // 15 Oct 2026, 14:30 local
const at = (y, m, d, h = 12, min = 0) => new Date(y, m, d, h, min).toISOString();

describe("date sections", () => {
  test("each boundary lands in Apple's section", () => {
    const label = (iso) => dateGroup(iso, now).label;
    assert.equal(label(at(2026, 9, 15, 0, 0)), "Today");
    assert.equal(label(at(2026, 9, 14, 23, 59)), "Yesterday");
    assert.equal(label(at(2026, 9, 13)), "Previous 7 Days");
    assert.equal(label(at(2026, 9, 8)), "Previous 7 Days");
    assert.equal(label(at(2026, 9, 7)), "Previous 30 Days");
    assert.equal(label(at(2026, 8, 15)), "Previous 30 Days");
    assert.equal(label(at(2026, 8, 14)), new Date(2026, 8, 14).toLocaleDateString(undefined, { month: "long" }));
    assert.equal(label(at(2025, 11, 31)), "2025");
  });

  test("a date ahead of this clock counts as today", () => {
    assert.equal(dateGroup(at(2026, 9, 16), now).label, "Today");
  });

  test("pinned first, then date sections in order", () => {
    const items = [
      { id: "p", pinned: true, updated_at: at(2025, 1, 1) },
      { id: "a", updated_at: at(2026, 9, 15, 9) },
      { id: "b", updated_at: at(2026, 9, 14) },
      { id: "c", updated_at: at(2026, 9, 1) },
    ];
    const sections = groupNotes(items, { now });
    assert.deepEqual(sections.map((s) => [s.label, s.items.map((i) => i.id)]), [
      ["Pinned", ["p"]],
      ["Today", ["a"]],
      ["Yesterday", ["b"]],
      ["Previous 30 Days", ["c"]],
    ]);
  });

  test("sorted by title or grouping off: one section after Pinned", () => {
    const items = [{ id: "x", updated_at: at(2026, 1, 1) }, { id: "y", updated_at: at(2026, 9, 15) }];
    assert.deepEqual(groupNotes(items, { now, sortBy: "title" }).map((s) => s.items.length), [2]);
    assert.deepEqual(groupNotes(items, { now, groupByDate: false }).map((s) => s.label), [null]);
  });

  test("created-date sorting groups by created date", () => {
    const items = [{ id: "x", created_at: at(2026, 9, 15), updated_at: at(2020, 0, 1) }];
    assert.equal(groupNotes(items, { now, sortBy: "created" })[0].label, "Today");
  });

  test("Recently Deleted does not pin", () => {
    const items = [{ id: "p", pinned: true, updated_at: at(2026, 9, 15) }];
    assert.equal(groupNotes(items, { now, pinning: false })[0].label, "Today");
  });
});

describe("row dates", () => {
  test("time today, Yesterday, weekday within a week, short date before", () => {
    assert.match(rowDate(at(2026, 9, 15, 9, 5), now), /9[:.]05/);
    assert.equal(rowDate(at(2026, 9, 14), now), "Yesterday");
    assert.equal(rowDate(at(2026, 9, 10), now), new Date(2026, 9, 10).toLocaleDateString(undefined, { weekday: "long" }));
    assert.equal(rowDate(at(2026, 6, 4), now), new Date(2026, 6, 4).toLocaleDateString(undefined, { day: "numeric", month: "numeric", year: "2-digit" }));
    assert.equal(rowDate(null, now), "");
  });
});
