/**
 * The Skills entry in the composer, and the panel it opens.
 *
 * The load-bearing assertion is the second one: Skills belongs to agent mode
 * only. A skill's instructions are fetched mid-run by a tool call, and a chat
 * reply has no loop to make one from -- so a Skills entry in the chat menu
 * would open a panel whose switches changed nothing about the next turn.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { ChatComposer } from "../src/App.jsx";

function render(overrides = {}) {
  const props = {
    disabled: false,
    value: "",
    onChange() {},
    onSubmit() {},
    llms: [{ id: "l1", name: "Local", model: "qwen3", enabled: true }],
    llmId: "l1",
    mode: "agent",
    onModeChange() {},
    onLlmChange() {},
    repos: [],
    selectedRepoId: "",
    onRepoChange() {},
    agentDefinitions: [],
    selectedAgentDefinitionId: "general",
    onAgentDefinitionChange() {},
    agentMode: "normal",
    onAgentModeChange() {},
    agentMessage: "",
    ...overrides,
  };
  return renderToStaticMarkup(createElement(ChatComposer, props));
}

describe("the Skills entry in the composer menu", () => {
  test("agent mode offers it", () => {
    const html = render();

    assert.ok(html.includes("agent-skills-button"), "missing the Skills button");
    assert.ok(html.includes(">Skills</span>"));
  });

  test("chat mode does not", () => {
    const html = render({ mode: "chatbot" });

    assert.ok(!html.includes("agent-skills-button"));
  });

  test("it is a menu action rather than a chip", () => {
    // Chips are the settings that ride on every turn; this opens a dialog, and
    // the agent-mode tests count chips by class.
    const html = render();
    const button = html.slice(html.indexOf("agent-skills-button"));

    assert.ok(button.startsWith("agent-skills-button"));
    assert.ok(html.includes('class="composer-menu-action agent-skills-button"'));
  });

  test("it is disabled along with everything else mid-submission", () => {
    const html = render({ disabled: true });
    const button = html.slice(html.indexOf("agent-skills-button"));

    assert.ok(button.slice(0, 200).includes('disabled=""'));
  });
});
