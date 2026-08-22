/**
 * The session view's pieces, rendered for real.
 *
 * These pin the distinctions the UI exists to make. The most important one is
 * that "the agent says it finished" and "the work was checked" look different
 * on screen -- collapsing them is exactly how a user ends up trusting an
 * unverified result.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import {
  ApprovalCard,
  ChangesPanel,
  STOP_REASON_COPY,
  TodoPanel,
  ToolCard,
} from "../src/AgentSession.jsx";

const render = (component, props) => renderToStaticMarkup(createElement(component, props));

describe("tool cards", () => {
  test("show the tool, a readable summary and a status", () => {
    const html = render(ToolCard, {
      event: { name: "read_file", summary: "Read app/main.py", status: "ok", duration_ms: 12 },
    });

    assert.ok(html.includes("read_file"));
    assert.ok(html.includes("Read app/main.py"));
    assert.ok(html.includes("12ms"));
  });

  test("collapse their output by default so a long run stays readable", () => {
    const html = render(ToolCard, {
      event: { name: "grep", status: "ok", content: "SECRET-OUTPUT-STRING" },
    });

    assert.ok(!html.includes("SECRET-OUTPUT-STRING"));
  });

  test("a failure is visually distinct from a success", () => {
    const failed = render(ToolCard, { event: { name: "run_tests", status: "error" } });
    const ok = render(ToolCard, { event: { name: "run_tests", status: "ok" } });

    assert.ok(failed.includes("agent-tool-card bad"));
    assert.ok(ok.includes("agent-tool-card ok"));
  });

  test("a plan-mode proposal reads as neither success nor failure", () => {
    assert.ok(render(ToolCard, { event: { name: "write_file", status: "proposed" } }).includes("agent-tool-card warn"));
  });
});

describe("approval card", () => {
  const approval = {
    id: "ap1",
    tool_name: "write_file",
    summary: "Write app/main.py",
    reason: "workspace_write requires approval in normal mode.",
    arguments: { path: "app/services/search/core.py", content: "x" },
    grantable: true,
  };

  test("shows the exact arguments the decision covers", () => {
    const html = render(ApprovalCard, { approval, busy: false, onDecide() {} });

    assert.ok(html.includes("Write app/main.py"));
    assert.ok(html.includes("app/services/search/core.py"), "the user must see what they approve");
  });

  test("offers a scoped always-allow only when the action is grantable", () => {
    const grantable = render(ApprovalCard, { approval, busy: false, onDecide() {} });
    const single = render(ApprovalCard, {
      approval: { ...approval, grantable: false },
      busy: false,
      onDecide() {},
    });

    assert.ok(grantable.includes("Allow always in app/services/search/"), "name the scope");
    assert.ok(!single.includes("Allow always"));
    assert.ok(single.includes("one call at a time"), "and it should say why");
  });

  // A file at the repository root has no enclosing folder, so the derived
  // prefix is empty -- and an empty prefix matches nothing. Offering it as a
  // folder scope produced a grant the server always refused.
  describe("a file at the repository root", () => {
    const rootApproval = {
      ...approval,
      summary: "Edit ab.py",
      arguments: { path: "ab.py", old_string: "a", new_string: "b" },
    };

    test("is granted for the repository rather than an empty folder", () => {
      const html = render(ApprovalCard, { approval: rootApproval, busy: false, onDecide() {} });

      assert.ok(html.includes("Allow always in this repository"));
      // The bug rendered a scope with nothing after "in", which the server then
      // refused; any empty-scope label is the regression.
      assert.ok(!/Allow always in\s*</.test(html), "an unnamed scope is the bug");
    });

    test("says why the scope is empty", () => {
      const html = render(ApprovalCard, { approval: rootApproval, busy: false, onDecide() {} });

      assert.ok(html.includes("at the repository root"));
    });

    test("still offers a narrower scope to type", () => {
      const html = render(ApprovalCard, { approval: rootApproval, busy: false, onDecide() {} });

      assert.ok(html.includes('aria-label="Grant path prefix"'), "narrowing stays possible");
      assert.ok(!html.includes('placeholder="app/services/"'), "no fake default scope");
    });
  });

  test("always offers allow-once and reject", () => {
    const html = render(ApprovalCard, { approval, busy: false, onDecide() {} });

    assert.ok(html.includes("Allow once"));
    assert.ok(html.includes("Reject"));
  });

  test("suggests the enclosing folder as the grant scope", () => {
    const html = render(ApprovalCard, { approval, busy: false, onDecide() {} });

    assert.ok(html.includes('placeholder="app/services/search/"'));
  });

  test("every action is disabled while a decision is in flight", () => {
    const html = render(ApprovalCard, { approval, busy: true, onDecide() {} });

    assert.equal(html.split('disabled=""').length - 1, 3);
  });
});

describe("todo panel", () => {
  test("is absent when the agent kept no checklist", () => {
    assert.equal(render(TodoPanel, { items: [] }), "");
    assert.equal(render(TodoPanel, { items: undefined }), "");
  });

  test("counts completed work", () => {
    const html = render(TodoPanel, {
      items: [
        { title: "find the ranker", status: "completed" },
        { title: "add a docstring", status: "in_progress" },
        { title: "run tests", status: "pending" },
      ],
    });

    assert.ok(html.includes("<span>1/3</span>"), "one of three done");
    assert.ok(html.includes("agent-todo-item completed"));
    assert.ok(html.includes("agent-todo-item in_progress"));
  });
});

describe("outcome wording", () => {
  test("verified and unverified completions are not the same message", () => {
    const verified = STOP_REASON_COPY.verified_complete;
    const unverified = STOP_REASON_COPY.unverified_complete;

    assert.notEqual(verified.label, unverified.label);
    assert.equal(verified.tone, "ok");
    assert.equal(unverified.tone, "warn");
    assert.ok(/review/i.test(unverified.detail), "an unverified result must invite review");
  });

  test("every stop reason the backend can emit has copy", () => {
    for (const reason of [
      "verified_complete",
      "unverified_complete",
      "blocked",
      "failed",
      "cancelled",
      "budget_exhausted",
    ]) {
      assert.ok(STOP_REASON_COPY[reason]?.label, `no copy for ${reason}`);
      assert.ok(STOP_REASON_COPY[reason]?.detail, `no detail for ${reason}`);
    }
  });
});


/**
 * The panel that used to end every run with four buttons -- View diff, Download
 * changed files, Download workspace, Discard -- because the agent had been
 * editing a copy the user could not reach. A live workspace has no such gap:
 * the files are already theirs, so the panel reports and offers to reverse,
 * and there is nothing to hand back.
 */
describe("changes panel", () => {
  const live = {
    mode: "live",
    root: "/Users/me/project",
    deliverable: [{ path: "ab.py", status: "created" }],
    blocked: [],
    undoable: true,
  };

  test("a live run says the work is already saved, and where", () => {
    const html = render(ChangesPanel, { sessionId: "s1", delivery: live });

    assert.ok(html.includes("Changes written to your folder"));
    assert.ok(html.includes("/Users/me/project"), "the user must see which folder changed");
    assert.ok(html.includes("ab.py"));
  });

  test("a live run offers no download and nothing to discard", () => {
    const html = render(ChangesPanel, { sessionId: "s1", delivery: live });

    assert.ok(!html.includes("Download changed files"));
    assert.ok(!html.includes("Download workspace"));
    assert.ok(!html.includes("Discard"));
    assert.ok(!html.includes("Apply changes"), "there is nothing left to apply");
  });

  test("a live run can be undone, and can still be read as a diff", () => {
    const html = render(ChangesPanel, { sessionId: "s1", delivery: live });

    assert.ok(html.includes("Undo this run"));
    assert.ok(html.includes("View diff"));
  });

  test("undo is unavailable when the run changed nothing", () => {
    const html = render(ChangesPanel, {
      sessionId: "s1",
      delivery: { ...live, undoable: false },
    });

    assert.ok(html.includes('Undo this run'));
    assert.ok(html.includes('disabled=""'), "an empty run must not offer a live undo");
  });

  test("a managed copy still has to be applied, and says so", () => {
    const html = render(ChangesPanel, {
      sessionId: "s1",
      delivery: {
        mode: "write_back",
        deliverable: [{ path: "app/main.py", status: "modified" }],
        blocked: [],
      },
    });

    assert.ok(html.includes("Deliver to your repository"));
    assert.ok(html.includes("Apply changes"));
    assert.ok(!html.includes("Undo this run"), "a copy has no user files to restore");
  });

  test("a file the user edited since import is shown as blocked, with the reason", () => {
    const html = render(ChangesPanel, {
      sessionId: "s1",
      delivery: {
        mode: "write_back",
        deliverable: [],
        blocked: [{ path: "app/main.py", reason: "This file changed in your repository." }],
      },
    });

    assert.ok(html.includes("This file changed in your repository."));
  });

  test("nothing renders when the run changed nothing at all", () => {
    const html = render(ChangesPanel, {
      sessionId: "s1",
      delivery: { mode: "live", root: "/x", deliverable: [], blocked: [] },
    });

    assert.equal(html, "");
  });
});
