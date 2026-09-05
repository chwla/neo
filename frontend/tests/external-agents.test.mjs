/**
 * The external-executor surface: connecting an engine, choosing one, and
 * reading a turn back.
 *
 * The rule the picker exists to enforce is that it offers engines that would
 * actually run the next turn -- signing in happens in Settings > Engines, and
 * an engine that has not been signed in to is absent rather than present and
 * broken. The exception is the engine this chat is already on, which is named
 * even when it has stopped working, because a select with no matching option
 * shows its first one and would claim the chat is on Neo.
 *
 * Beyond that: a turn has to say which engine produced it -- scrolling back
 * through a long coding session is exactly when that stops being obvious.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { ChatComposer, engineOptions } from "../src/App.jsx";
import ExternalAgents, { engineState } from "../src/ExternalAgents.jsx";
import { ExecutorBadge, StepDivider, entryFromEvent, formatCost } from "../src/AgentTurn.jsx";
import { applyEvent } from "../src/chatStream.js";

const CAPS = (overrides = {}) => ({
  resume: true,
  plan_mode: true,
  tool_denylist: true,
  per_tool_approval: false,
  cost_reporting: true,
  token_reporting: true,
  ...overrides,
});

const EXTERNAL = [
  {
    id: "claude_code",
    name: "Claude Code",
    available: true,
    reason: null,
    capabilities: CAPS(),
    notes: ["Neo cannot approve individual tool calls for this engine."],
  },
  {
    id: "codex",
    name: "Codex",
    available: false,
    reason: "not signed in -- run `codex login`",
    capabilities: CAPS({ tool_denylist: false, cost_reporting: false }),
    notes: [
      "Neo cannot approve individual tool calls for this engine.",
      "Per-chat tool toggles do not apply to this engine.",
    ],
  },
];

function composer(overrides = {}) {
  const props = {
    disabled: false,
    value: "",
    onChange() {},
    onSubmit() {},
    llms: [{ id: "l1", name: "Local", model: "qwen3", enabled: true }],
    llmId: "l1",
    onLlmChange() {},
    mode: "agent",
    onModeChange() {},
    repos: [{ id: "r1", name: "neo" }],
    selectedRepoId: "r1",
    onRepoChange() {},
    agentDefinitions: [{ id: "builtin-general", name: "general", display_name: "General" }],
    selectedAgentDefinitionId: "general",
    onAgentDefinitionChange() {},
    agentMode: "normal",
    onAgentModeChange() {},
    executor: "neo",
    onExecutorChange() {},
    externalAgents: EXTERNAL,
    agentMessage: "",
    ...overrides,
  };
  return renderToStaticMarkup(createElement(ChatComposer, props));
}

describe("the engine chip", () => {
  test("offers Neo and the engines that are actually connected", () => {
    const html = composer();
    assert.ok(html.includes('<span class="agent-chip-label">Engine</span>'));
    assert.ok(html.includes('aria-label="Select agent engine"'));
    assert.ok(html.includes('value="neo"'));
    assert.ok(html.includes('value="claude_code"'), "Claude Code is signed in, so it is offered");
  });

  test("an engine that has not been signed in to is not offered at all", () => {
    // The whole point of the change. Codex is installed but signed out, so it
    // would fail the next turn; offering it makes the picker a place where
    // work happens, and the work belongs in Settings > Engines.
    const html = composer();
    assert.ok(!html.includes('value="codex"'), "a signed-out engine is absent, not disabled");
  });

  test("the way to connect one is named where the engines would have been", () => {
    // An option that vanishes teaches nothing on its own; the picker has to
    // point at the place that brings it back.
    assert.ok(composer({ externalAgents: [] }).includes("Connect Claude Code or Codex"));
    assert.ok(composer().includes("Manage engines in Settings"));
  });

  test("only connected engines are options", () => {
    assert.deepEqual(
      engineOptions("neo", EXTERNAL).map((option) => option.id),
      ["neo", "claude_code"],
    );
  });

  test("the engine this chat already runs on is offered even when it broke", () => {
    // Otherwise the select falls back to showing its first option and tells
    // someone their chat is on Neo, one turn before it is refused.
    const options = engineOptions("codex", EXTERNAL);
    assert.deepEqual(options.map((option) => option.id), ["neo", "claude_code", "codex"]);
    assert.equal(options.at(-1).available, false);
    assert.equal(options.at(-1).name, "Codex", "the label stays the name; the state is said below");
  });

  test("and the chat being on a broken engine is said in full underneath", () => {
    // Not in the option label: the chip truncates at 160px, and "Codex - not
    // con..." is not a warning, it is a puzzle.
    const html = composer({ executor: "codex" });
    assert.ok(html.includes("Codex is not connected"), "it names the engine and the problem");
    assert.ok(html.includes("agent-engine-link is-broken"), "and stops being a faint aside");
  });

  test("a working engine gets no such warning", () => {
    assert.ok(!composer({ executor: "claude_code" }).includes("is not connected"));
    assert.ok(!composer({ executor: "neo" }).includes("is not connected"));
  });

  test("so is one Neo cannot detect at all", () => {
    const options = engineOptions("gone", []);
    assert.deepEqual(options.map((option) => option.id), ["neo", "gone"]);
    assert.equal(options.at(-1).available, false);
  });

  test("the chosen engine is what the select shows", () => {
    const html = composer({ executor: "claude_code" });
    assert.ok(html.includes('<option value="claude_code"'));
    assert.ok(html.includes("Claude Code"));
  });

  test("no engine chip leaks into chat mode", () => {
    const html = composer({ mode: "chat" });
    assert.ok(!html.includes('<span class="agent-chip-label">Engine</span>'));
  });

  test("it renders with no external executors at all", () => {
    // The feature is off by default, and Agent mode must stay fully usable.
    const html = composer({ externalAgents: [] });
    assert.ok(html.includes('<span class="agent-chip-label">Engine</span>'));
    assert.ok(html.includes('value="neo"'));
  });
});

describe("Settings > Engines is where an engine is connected", () => {
  // The four states a row can be in, in the order they have to be fixed:
  // nothing is signed in that is not installed, and nothing runs while the
  // profile has not opted in. `available` on this endpoint is the machine fact
  // -- installed and signed in -- with the profile switch passed separately.
  test("a CLI that is not installed reports that first", () => {
    assert.equal(engineState({ version: null, available: false }, true), "not_installed");
    assert.equal(engineState({ version: null, available: false }, false), "not_installed");
  });

  test("installed but signed out is its own state, with its own button", () => {
    assert.equal(engineState({ version: "2.1.0", available: false }, true), "signed_out");
  });

  test("signed in with the feature off is not the same as unavailable", () => {
    // The distinction the setup endpoint exists for: nothing is wrong with the
    // CLI, and the button says "Turn on" rather than sending someone to a
    // sign-in they have already done.
    assert.equal(engineState({ version: "2.1.0", available: true }, false), "off");
  });

  test("installed, signed in and switched on is connected", () => {
    assert.equal(engineState({ version: "2.1.0", available: true }, true), "connected");
  });

  test("the panel states what it is for before it has loaded anything", () => {
    const html = renderToStaticMarkup(createElement(ExternalAgents, { onClose() {} }));
    assert.ok(html.includes("Engine picker") || html.includes("Engine"), "it names the picker");
    assert.ok(html.includes("Allow external engines"), "the profile switch is present");
  });
});

describe("the executor badge", () => {
  const badge = (executor, meta) =>
    renderToStaticMarkup(createElement(ExecutorBadge, { executor, meta }));

  test("names the engine that produced the turn", () => {
    assert.ok(badge("claude_code", {}).includes("Claude Code"));
    assert.ok(badge("codex", {}).includes("Codex"));
  });

  test("Neo's own runs are not badged", () => {
    assert.equal(badge("neo", {}), "");
    assert.equal(badge(undefined, {}), "");
  });

  test("cost is shown when the CLI reported one", () => {
    const html = badge("claude_code", { total_cost_usd: 0.42, num_turns: 3 });
    assert.ok(html.includes("$0.42"));
    assert.ok(html.includes("3 turns"));
  });

  test("no cost is invented when the CLI reported none", () => {
    // Codex reports tokens only. A fabricated price would be worse than silence.
    const html = badge("codex", { usage: { input_tokens: 100 } });
    assert.ok(!html.includes("$"));
  });
});

describe("handoff dividers", () => {
  test("a step event becomes a divider entry", () => {
    const entry = entryFromEvent({
      type: "step.started",
      executor: "codex",
      name: "Codex",
      role: "Build",
      index: 1,
      total: 2,
    });
    assert.equal(entry.kind, "step");
    assert.equal(entry.id, "step-1");
  });

  test("the first step names the engine and later steps say handed to", () => {
    const first = renderToStaticMarkup(
      createElement(StepDivider, { event: { executor: "claude_code", name: "Claude Code", index: 0, role: "Plan" } }),
    );
    assert.ok(first.includes("Claude Code"));
    assert.ok(!first.includes("handed to"));

    const second = renderToStaticMarkup(
      createElement(StepDivider, { event: { executor: "codex", name: "Codex", index: 1, role: "Build" } }),
    );
    assert.ok(second.includes("handed to Codex"));
    assert.ok(second.includes("Build"));
  });
});

describe("the stream reducer carries external turns", () => {
  const feed = (events) => events.reduce(applyEvent, new Map());

  test("a step divider lands in the same entry list as the work", () => {
    const state = feed([
      { type: "run.started", chat_id: 1, agent_session_id: "s1", seq: 1 },
      { type: "step.started", chat_id: 1, agent_session_id: "s1", seq: 2, executor: "claude_code", name: "Claude Code", index: 0 },
      { type: "tool.call", chat_id: 1, agent_session_id: "s1", seq: 3, call_id: "c1", name: "Read" },
      { type: "step.started", chat_id: 1, agent_session_id: "s1", seq: 4, executor: "codex", name: "Codex", index: 1 },
    ]);
    const turn = state.get(1);
    const kinds = turn.entries.map((entry) => entry.kind);
    assert.deepEqual(kinds, ["step", "tool", "step"], "the trace must read in order");
  });

  test("an external turn queued by the sub-cap shows as queued", () => {
    const state = feed([
      {
        type: "turn.queued",
        chat_id: 2,
        agent_session_id: "s2",
        seq: 1,
        reason: "external_cap",
        limit: 1,
      },
    ]);
    assert.equal(state.get(2).sessionStatus, "queued");
    assert.ok(state.get(2).statusText.toLowerCase().includes("queued"));
  });
});

describe("the security boundary is stated, not implied", () => {
  test("selecting an external engine explains what Neo cannot do", () => {
    // Collapsed rather than open -- it used to be the biggest thing in the menu
    // and the only thing that changed when an engine connected, which made
    // success look like failure. Still stated, still in full, still in the DOM.
    const html = composer({ executor: "claude_code" });
    assert.ok(html.includes("own permissions"), "the boundary must be named");
    assert.ok(html.includes("cannot approve individual tool calls"), "and stated in full");
  });

  test("Neo's own engine carries no such notice", () => {
    const html = composer({ executor: "neo" });
    assert.ok(!html.includes("runs under its own CLI"));
  });

  test("the tools control says when its toggles do not govern the engine", () => {
    // Codex has no tool denylist, so presenting Neo's toggles as though they
    // constrained it would assert an enforcement that does not exist.
    const html = composer({ executor: "codex" });
    assert.ok(html.includes("Tools (Neo turns only)"));
    assert.ok(html.includes("manages its own tools"));
  });

  test("an engine that does support a denylist keeps the plain control", () => {
    const html = composer({ executor: "claude_code" });
    assert.ok(html.includes(">Tools<"));
    assert.ok(!html.includes("Tools (Neo turns only)"));
  });

  test("Neo's own turns keep the plain control too", () => {
    const html = composer({ executor: "neo" });
    assert.ok(!html.includes("Tools (Neo turns only)"));
  });
});

describe("cost is never fabricated", () => {
  test("an unreported cost renders nothing at all", () => {
    // undefined means "this engine does not report cost", which is not zero.
    assert.equal(formatCost(undefined), null);
    assert.equal(formatCost(null), null);
    assert.equal(formatCost(Number.NaN), null);
  });

  test("a genuine sub-cent charge is not rounded down to free", () => {
    assert.equal(formatCost(0.004), "<$0.01");
    assert.equal(formatCost(0.12068), "$0.12");
  });

  test("a reported zero is shown as zero", () => {
    assert.equal(formatCost(0), "$0.00");
  });

  test("Codex shows tokens and no price", () => {
    const html = renderToStaticMarkup(
      createElement(ExecutorBadge, {
        executor: "codex",
        meta: { prompt_tokens: 63106, completion_tokens: 639 },
      }),
    );
    assert.ok(html.includes("Codex"));
    assert.ok(html.includes("tokens"), "token usage is reported and should be shown");
    assert.ok(!html.includes("$"), "Codex reports no cost; none may be displayed");
  });

  test("Claude shows both", () => {
    const html = renderToStaticMarkup(
      createElement(ExecutorBadge, {
        executor: "claude_code",
        meta: { total_cost_usd: 0.134957, prompt_tokens: 10, completion_tokens: 20, num_turns: 3 },
      }),
    );
    assert.ok(html.includes("$0.13"));
    assert.ok(html.includes("30 tokens"));
    assert.ok(html.includes("3 turns"));
  });
});
