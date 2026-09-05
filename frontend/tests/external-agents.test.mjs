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

import {
  ChatComposer,
  ChatMessage,
  engineOptions,
  withEngineSetting,
} from "../src/App.jsx";
import ExternalAgents, { engineState } from "../src/ExternalAgents.jsx";
import {
  AgentBubble,
  StepDivider,
  entryFromEvent,
  executorFacts,
  formatCost,
  senderName,
} from "../src/AgentTurn.jsx";
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

describe("the model chip", () => {
  const CATALOGUE = {
    default: "opus[1m]",
    options: [{ id: "sonnet", source: "documented" }, { id: "fable", source: "documented" }],
    effort_default: "xhigh",
    efforts: ["low", "medium", "high", "max"],
  };

  test("there is no model chip while Neo is the engine", () => {
    // Neo's own turns are chosen by the LLM picker, which is a different
    // control with a different list; two of them would be one too many.
    assert.ok(!composer({ executor: "neo" }).includes("Select engine model"));
  });

  test("an external engine gets one", () => {
    const html = composer({ executor: "claude_code", engineModels: CATALOGUE });
    assert.ok(html.includes('<span class="agent-chip-label">Model</span>'));
    assert.ok(html.includes('aria-label="Select engine model"'));
  });

  test("the CLI's own setting leads, named as the model it is", () => {
    // Not "Default · opus[1m]": the word said nothing the model name did not,
    // and the useful thing to read is what the turn will actually run on.
    const html = composer({ executor: "claude_code", engineModels: CATALOGUE });
    assert.ok(html.includes(">opus[1m]</option>"));
    assert.ok(!html.includes("Default"), "the word is gone from the chip");
    assert.ok(html.includes(">sonnet</option>"));
  });

  test("the account's own models are offered, not just documented aliases", () => {
    // Codex caches the account's list; that is a stronger answer than a
    // vocabulary parsed out of help text, and it is what makes the chip useful.
    const html = composer({
      executor: "codex",
      engineModels: {
        default: "gpt-5.6-sol",
        options: [{ id: "gpt-5.5", source: "account" }, { id: "gpt-5.4-mini", source: "account" }],
        effort_default: "high",
        efforts: [],
      },
    });
    assert.ok(html.includes(">gpt-5.5</option>"));
    assert.ok(html.includes(">gpt-5.4-mini</option>"));
  });

  test("an empty value means the default, and is the first option", () => {
    const html = composer({ executor: "claude_code", engineModels: CATALOGUE });
    const chip = html.slice(html.indexOf('aria-label="Select engine model"'));
    assert.ok(chip.indexOf('value=""') < chip.indexOf('value="sonnet"'));
  });

  test("nothing discovered still renders a usable chip", () => {
    // Nothing on the machine named a model, so there is no name to show. The
    // empty value still means "send no flag", which is what "Automatic" says.
    const html = composer({ executor: "codex", engineModels: { default: null, options: [] } });
    assert.ok(html.includes('<span class="agent-chip-label">Model</span>'));
    assert.ok(html.includes(">Automatic</option>"));
  });

  test("and so does a catalogue that never arrived", () => {
    const html = composer({ executor: "codex", engineModels: null });
    assert.ok(html.includes(">Automatic</option>"));
  });
});

describe("the effort chip", () => {
  const CATALOGUE = {
    default: "opus[1m]",
    options: [],
    effort_default: "xhigh",
    efforts: ["low", "medium", "high", "max"],
  };

  test("an external engine gets one, because both CLIs have a real one", () => {
    // `claude --effort <level>` and Codex's `model_reasoning_effort`. Neo's own
    // agent turns still have none: `effort` there only governs replies.
    const html = composer({ executor: "claude_code", engineModels: CATALOGUE });
    assert.ok(html.includes('aria-label="Select engine effort"'));
    assert.ok(html.includes(">max</option>"));
  });

  test("the CLI's own level leads, named as the level it is", () => {
    const html = composer({ executor: "claude_code", engineModels: CATALOGUE });
    const chip = html.slice(html.indexOf('aria-label="Select engine effort"'));
    assert.ok(chip.startsWith('aria-label="Select engine effort"'));
    assert.ok(chip.includes(">xhigh</option>"));
  });

  test("Neo's own engine gets none", () => {
    assert.ok(!composer({ executor: "neo" }).includes("Select engine effort"));
  });

  test("an engine that names no levels still offers its configured one", () => {
    const html = composer({
      executor: "codex",
      engineModels: { default: null, options: [], effort_default: "high", efforts: [] },
    });
    assert.ok(html.includes(">high</option>"));
  });
});

describe("clearing a per-engine setting", () => {
  test("a choice is stored under its engine", () => {
    assert.deepEqual(withEngineSetting({}, "codex", "gpt-5.5"), { codex: "gpt-5.5" });
  });

  test("clearing removes the key rather than storing an empty one", () => {
    // So "leave the CLI alone" has one representation, and can never reach an
    // adapter as `--model ""`.
    assert.deepEqual(withEngineSetting({ codex: "gpt-5.5" }, "codex", ""), {});
  });

  test("the other engine's choice is untouched either way", () => {
    const both = { codex: "gpt-5.5", claude_code: "opus" };
    assert.deepEqual(withEngineSetting(both, "codex", ""), { claude_code: "opus" });
    assert.deepEqual(withEngineSetting(both, "codex", "gpt-5.4-mini"), {
      codex: "gpt-5.4-mini",
      claude_code: "opus",
    });
  });

  test("a missing map is not a crash", () => {
    assert.deepEqual(withEngineSetting(undefined, "codex", "gpt-5.5"), { codex: "gpt-5.5" });
  });
});

describe("whose model answers the turn", () => {
  test("Neo's LLM picker is gone once an external engine runs agent turns", () => {
    // It would be a control the turn ignores, sitting where the answer's
    // author is named. Claude Code and Codex answer on their own model.
    assert.ok(!composer({ mode: "agent", executor: "claude_code" }).includes("Choose LLM"));
  });

  test("it stays for Neo's own agent turns", () => {
    assert.ok(composer({ mode: "agent", executor: "neo" }).includes("Choose LLM"));
  });

  test("and it stays in chat mode whatever the chat's engine is", () => {
    // The engine governs agent turns only; a plain reply is always Neo's.
    assert.ok(composer({ mode: "chat", executor: "claude_code" }).includes("Choose LLM"));
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

describe("the engine signs the turn it produced", () => {
  // It used to be signed "Neo", with a pale pill above the bubble correcting
  // that -- two names for one turn, the wrong one in the slot a reader actually
  // reads the author from. Now there is one name, in that slot.
  test("an external turn is signed by the engine that ran it", () => {
    assert.equal(senderName("claude_code"), "Claude Code");
    assert.equal(senderName("codex"), "Codex");
  });

  test("Neo's own turns are still signed Neo", () => {
    assert.equal(senderName("neo"), "Neo");
    assert.equal(senderName(undefined), "Neo");
    assert.equal(senderName(""), "Neo");
  });

  test("an engine Neo has no name for is signed with its id, not with Neo's name", () => {
    assert.equal(senderName("something_else"), "something_else");
  });

  test("the bubbles inside the turn carry the same signature", () => {
    const html = renderToStaticMarkup(
      createElement(AgentBubble, { role: "assistant", text: "done", sender: "Codex" }),
    );
    assert.ok(html.includes('<span class="message-sender">Codex</span>'));
    assert.ok(!html.includes(">Neo<"));
  });

  test("and a bubble with no engine named falls back to Neo", () => {
    const html = renderToStaticMarkup(createElement(AgentBubble, { role: "assistant", text: "x" }));
    assert.ok(html.includes('<span class="message-sender">Neo</span>'));
  });
});

describe("the turn in the transcript", () => {
  // The whole thing end to end: one finished agent turn, as the chat draws it.
  const turn = (executor, meta) =>
    renderToStaticMarkup(
      createElement(ChatMessage, {
        message: {
          id: 7,
          role: "assistant",
          content: "Done.",
          response_kind: "agent_run",
          created_at: "2026-09-05T13:00:00",
          duration_ms: 2300,
          model_name: "claude-opus-5[1m]",
          provider_name: executor,
        },
        messages: [],
        agentRun: {
          session: { id: "s1", status: "succeeded", executor, external_meta: { [executor]: meta } },
        },
        agentEntries: [],
      }),
    );

  test("is signed by the engine, where every other reply signs Neo", () => {
    const html = turn("claude_code", { total_cost_usd: 0.07, num_turns: 1 });
    assert.ok(html.includes('<span class="message-sender">Claude Code</span>'));
    assert.ok(!html.includes('<span class="message-sender">Neo</span>'));
  });

  test("carries the cost in the footer it already had, not in a badge", () => {
    const html = turn("claude_code", { total_cost_usd: 0.07, num_turns: 1 });
    assert.ok(html.includes("$0.07"), "the cost survived the badge");
    assert.ok(html.includes("1 turns"));
    assert.ok(!html.includes("agent-executor-badge"), "and the badge is gone");
  });

  test("a Codex turn is signed Codex and priced at nothing", () => {
    const html = turn("codex", { prompt_tokens: 63106 });
    assert.ok(html.includes('<span class="message-sender">Codex</span>'));
    assert.ok(!html.includes("$"));
  });

  test("a plain reply is untouched", () => {
    const html = renderToStaticMarkup(
      createElement(ChatMessage, {
        message: { id: 8, role: "assistant", content: "Hi.", created_at: "2026-09-05T13:00:00" },
        messages: [],
      }),
    );
    assert.ok(html.includes('<span class="message-sender">Neo</span>'));
  });
});

describe("what the badge carried that the message footer does not", () => {
  // Tokens and duration are stored on the turn row and printed by every reply's
  // footer already; cost and the CLI's own turn count are not, and losing them
  // with the badge would lose the only record of what a run cost.
  test("cost and turn count are handed to the footer", () => {
    assert.deepEqual(executorFacts("claude_code", { total_cost_usd: 0.42, num_turns: 3 }), [
      "$0.42",
      "3 turns",
    ]);
  });

  test("tokens are not, because the footer already prints its own count", () => {
    const facts = executorFacts("claude_code", { prompt_tokens: 900, completion_tokens: 100 });
    assert.ok(!facts.some((fact) => fact.includes("tokens")));
  });

  test("no cost is invented when the CLI reported none", () => {
    // Codex reports tokens only. A fabricated price would be worse than silence.
    assert.deepEqual(executorFacts("codex", { usage: { input_tokens: 100 } }), []);
  });

  test("Neo's own runs contribute nothing", () => {
    assert.deepEqual(executorFacts("neo", { total_cost_usd: 1 }), []);
    assert.deepEqual(executorFacts(undefined, { total_cost_usd: 1 }), []);
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

  test("Codex contributes no price to the footer", () => {
    assert.deepEqual(
      executorFacts("codex", { prompt_tokens: 63106, completion_tokens: 639 }),
      [],
      "Codex reports no cost; none may be displayed",
    );
  });

  test("Claude contributes the price it reported", () => {
    assert.deepEqual(
      executorFacts("claude_code", {
        total_cost_usd: 0.134957,
        prompt_tokens: 10,
        completion_tokens: 20,
        num_turns: 3,
      }),
      ["$0.13", "3 turns"],
    );
  });
});
