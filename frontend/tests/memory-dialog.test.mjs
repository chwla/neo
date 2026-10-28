import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { belongsToProject, onProjectsTab } from "../src/MemoryDialog.jsx";

const global = { field: "miscellaneous", scope: { type: "global" }, learned_in: [] };
const learnedInRandom = {
  field: "miscellaneous",
  scope: { type: "global" },
  learned_in: [{ project_id: "2", project_name: "random" }],
};
const scopedToDsa = {
  field: "projects",
  scope: { type: "project", project_id: "1", project_name: "dsa" },
  learned_in: [],
};

describe("the projects tab", () => {
  test("shows a personal fact under the project it was learned in", () => {
    // It stays global -- the scope is untouched -- but it came from there.
    assert.ok(onProjectsTab(learnedInRandom));
    assert.ok(belongsToProject(learnedInRandom, "2"));
    assert.ok(!belongsToProject(learnedInRandom, "1"));
  });

  test("shows a project-scoped memory under its project", () => {
    assert.ok(onProjectsTab(scopedToDsa));
    assert.ok(belongsToProject(scopedToDsa, "1"));
    assert.ok(!belongsToProject(scopedToDsa, "2"));
  });

  test("leaves a memory with no project out of it", () => {
    assert.ok(!onProjectsTab(global));
    assert.ok(!onProjectsTab({ ...global, learned_in: undefined }));
  });
});
