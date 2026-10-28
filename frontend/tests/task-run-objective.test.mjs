/*
 * Starting an agent run on a task must not echo the task's stored text back to
 * the server as though the user had typed it. A task made from a private GitHub
 * issue is private; the server labels an objective it loaded itself, but cannot
 * tell a string the browser sends from one the user typed.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { taskRunPayload } from "../src/Tasks.jsx";

const task = {
  id: "task-1",
  project_id: "proj-1",
  title: "Rotate staging credentials",
  description: "NEOCANARY-PRIVATE-ISSUE: the staging password rotates on Friday",
};

test("with nothing typed, the server is asked to load the task's text", () => {
  const payload = taskRunPayload(task, "", "normal");
  assert.equal(payload.use_task_objective, true);
  assert.equal("objective" in payload, false);
  assert.equal(JSON.stringify(payload).includes("NEOCANARY"), false);
  assert.equal(JSON.stringify(payload).includes(task.title), false);
});

test("whitespace is not a typed objective", () => {
  const payload = taskRunPayload(task, "   \n ", "normal");
  assert.equal(payload.use_task_objective, true);
  assert.equal("objective" in payload, false);
});

test("text the user typed is sent as their objective", () => {
  const payload = taskRunPayload(task, "  Summarise the open issues ", "auto");
  assert.equal(payload.objective, "Summarise the open issues");
  assert.equal("use_task_objective" in payload, false);
  assert.equal(payload.mode, "auto");
});

test("the task and project are always linked", () => {
  for (const typed of ["", "do it"]) {
    const payload = taskRunPayload(task, typed, "normal");
    assert.equal(payload.task_id, "task-1");
    assert.equal(payload.project_id, "proj-1");
  }
  assert.equal(taskRunPayload({ id: "t2" }, "", "normal").project_id, null);
});
