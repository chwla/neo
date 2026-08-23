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
  AgentBubble,
  ApprovalCard,
  DiffView,
  entryFromEvent,
  groupEntries,
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

/**
 * What the run produced is reachable from the message that reports it -- View
 * diff, Undo this run -- so the panel that used to restate all of that under
 * every finished run is gone. What remains is the diff itself, on demand.
 */
describe("the diff", () => {
  test("nothing renders until one is asked for", () => {
    assert.equal(render(DiffView, { patch: "" }), "");
  });

  test("a requested diff is shown verbatim", () => {
    const html = render(DiffView, { patch: "--- a/x.py\n+++ b/x.py\n+print(1)" });

    assert.ok(html.includes("+++ b/x.py"));
    assert.ok(html.includes("+print(1)"));
    assert.ok(html.includes("Close the diff"), "a diff must be dismissable");
  });
});

/**
 * The transcript's footer is built from the event, so the event's record has to
 * survive the trip. Keeping only `content` here left every agent message with a
 * bare "..." and no time, model, token count or duration.
 */
describe("what a streamed turn carries", () => {
  const chunk = {
    type: "chunk",
    seq: 4,
    content: "Looking at the repository.",
    created_at: "2026-08-22T21:35:09",
    provider_name: "ollama",
    model_name: "gemma4:latest",
    total_tokens: 968,
    duration_ms: 9000,
  };

  test("the turn's record reaches the entry, not just its prose", () => {
    const entry = entryFromEvent(chunk);

    assert.equal(entry.kind, "text");
    assert.equal(entry.created_at, chunk.created_at);
    assert.equal(entry.provider_name, "ollama");
    assert.equal(entry.model_name, "gemma4:latest");
    assert.equal(entry.total_tokens, 968);
    assert.equal(entry.duration_ms, 9000);
  });

  test("an empty chunk is not an entry", () => {
    assert.equal(entryFromEvent({ type: "chunk", seq: 1, content: "" }), null);
  });

  test("merging two turns into one bubble sums what they cost", () => {
    const [merged] = groupEntries([
      entryFromEvent(chunk),
      entryFromEvent({ ...chunk, seq: 5, content: " Now the README.", total_tokens: 32, duration_ms: 1000 }),
    ]);

    assert.equal(merged.content, "Looking at the repository. Now the README.");
    assert.equal(merged.total_tokens, 1000, "both turns' tokens");
    assert.equal(merged.duration_ms, 10000, "both turns' time");
  });

  test("the footer renders the record the entry carries", () => {
    const html = render(AgentBubble, { role: "assistant", text: "done", entry: entryFromEvent(chunk) });

    assert.ok(html.includes("ollama / gemma4:latest"), "the model that answered");
    assert.ok(html.includes("968 tokens"));
    assert.ok(html.includes("9.0 s"));
    assert.ok(/<time class="message-time">[^<]+<\/time>/.test(html), "a timestamp");
    assert.ok(html.includes("Response actions"), "the actions menu");
  });

  test("a user turn is stamped but carries no model record", () => {
    const html = render(AgentBubble, {
      role: "user",
      text: "verify this code",
      entry: { created_at: "2026-08-22T21:34:00", content: "verify this code" },
    });

    assert.ok(/<time class="message-time">[^<]+<\/time>/.test(html));
    assert.ok(!html.includes("tokens"));
    assert.ok(html.includes("Message actions"));
  });
});
