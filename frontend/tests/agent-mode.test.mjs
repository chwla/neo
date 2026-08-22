/**
 * Agent mode's composer, rendered for real and read back as markup.
 *
 * The previous version of this file pinned a composer that planned a checklist
 * of persistent Tasks before any work started. That flow is gone, so these pin
 * what replaced it: the objective goes straight to a session, and the controls
 * that decide how much autonomy it has are visible before you press Start.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { ChatComposer, Modal } from "../src/App.jsx";

const PROJECTS = [
  { id: "p1", title: "Neo Platform" },
  { id: "p2", title: "Docs Refresh" },
];
const REPOS = [
  { id: "r1", name: "neo" },
  { id: "r2", name: "docs" },
];
const AGENTS = [
  { id: "builtin-general", name: "general", display_name: "General" },
  { id: "a1", name: "reviewer", display_name: "Reviewer" },
  { id: "a2", name: "researcher", display_name: "" },
];

function render(overrides = {}) {
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
    projects: PROJECTS,
    selectedProjectId: "",
    onProjectChange() {},
    repos: REPOS,
    selectedRepoId: "",
    onRepoChange() {},
    agentDefinitions: AGENTS,
    selectedAgentDefinitionId: "general",
    onAgentDefinitionChange() {},
    agentMode: "normal",
    onAgentModeChange() {},
    agentMessage: "",
    ...overrides,
  };
  return renderToStaticMarkup(createElement(ChatComposer, props));
}

const count = (haystack, needle) => haystack.split(needle).length - 1;

describe("agent mode composer", () => {
  test("offers project, repo, mode and agent as labelled chips", () => {
    const html = render();

    assert.equal(count(html, 'class="agent-chip"'), 4);
    for (const label of ["Project", "Repo", "Mode", "Agent"]) {
      assert.ok(
        html.includes(`<span class="agent-chip-label">${label}</span>`),
        `missing ${label} chip`,
      );
    }
  });

  test("a folder on this computer can be opened for the agent to work in", () => {
    const html = render();

    assert.ok(html.includes("agent-chip-action"), "folder chip is missing");
    assert.ok(html.includes('<span class="agent-chip-label">Folder</span>'));
    assert.ok(html.includes(">Open</span>"));
    assert.ok(
      html.includes("edits it directly"),
      "the chip must say the agent edits the real folder, not a copy",
    );
  });

  test("the folder chip reports an attach in progress", () => {
    const html = render({ folderAttaching: true });

    assert.ok(html.includes("Opening"), "an in-flight attach must be visible");
    assert.ok(!html.includes(">Open</span>"), "idle label must give way");
  });

  test("permission mode is chosen before the run starts", () => {
    const html = render();

    assert.ok(html.includes('value="plan"'));
    assert.ok(html.includes('value="normal"'));
    assert.ok(html.includes('value="auto"'));
    assert.ok(html.includes("propose only"), "plan mode must say it changes nothing");
    assert.ok(html.includes("ask first"), "normal mode must say it pauses for approval");
  });

  test("the selected mode is the one marked selected", () => {
    const html = render({ agentMode: "auto" });

    assert.ok(html.includes('<option value="auto" selected='));
  });

  test("no repository is a valid choice", () => {
    const html = render();

    assert.ok(html.includes("No repository"));
    assert.ok(html.includes("No project"));
  });

  test("repositories are listed by name", () => {
    const html = render();

    assert.ok(html.includes(">neo</option>"));
    assert.ok(html.includes(">docs</option>"));
  });

  test("General is listed exactly once", () => {
    const html = render();

    assert.equal(count(html, ">General</option>"), 1);
  });

  test("an agent without a display name falls back to its name", () => {
    const html = render();

    assert.ok(html.includes(">researcher</option>"));
  });

  test("there is a single Start action and no planning step", () => {
    const html = render({ value: "add a docstring" });

    assert.ok(html.includes('aria-label="Start Agent"'));
    assert.ok(!html.includes(">Plan</button>"), "the checklist step was removed");
    assert.ok(!html.includes("agent-plan-preview"));
    assert.ok(!html.includes("agent-created-tasks"));
  });

  test("Start is disabled until there is an objective", () => {
    assert.ok(render({ value: "   " }).includes("disabled=\"\""));
    assert.ok(!render({ value: "do the thing" }).includes('class="agent-run-button primary" disabled'));
  });

  test("the hint describes autonomy rather than a checklist", () => {
    const html = render({ selectedRepoId: "r1" });

    assert.ok(html.includes("agent-mode-hint"));
    assert.ok(!html.toLowerCase().includes("checklist"), "the run no longer creates a checklist up front");
    assert.ok(html.includes("undone"), "the hint should say the run is reversible");
  });

  test("the hint gives way to the objective once one is typed", () => {
    const html = render({ value: "do the thing", selectedRepoId: "r1" });

    assert.ok(!html.includes("agent-mode-hint"));
  });

  // Without a repository the agent has no file or command tools, so a run could
  // only narrate work it cannot do. The composer has to say so and refuse.
  test("without a repository the composer asks for one", () => {
    const html = render({ selectedRepoId: "" });

    assert.ok(html.includes("Select a workspace first"));
    assert.ok(html.includes("Open a folder"), "it must name the way out, not just the problem");
  });

  test("the repository prompt outranks the objective hint", () => {
    const html = render({ selectedRepoId: "", value: "do the thing" });

    assert.ok(html.includes("Select a workspace first"), "a typed objective must not hide it");
  });

  test("Start is disabled until a repository is chosen", () => {
    const typed = { value: "do the thing" };

    assert.ok(
      render({ ...typed, selectedRepoId: "" }).includes('title="Select a repository first"'),
      "the disabled Start must explain itself",
    );
    assert.ok(render({ ...typed, selectedRepoId: "r1" }).includes('title="Start Agent"'));
  });

  test("a status message is surfaced when present", () => {
    assert.ok(render({ agentMessage: "Could not start the agent" }).includes("Could not start the agent"));
  });

  test("the coding workbench is gone; Agent Mode is the coding workflow", () => {
    const html = render();

    assert.ok(!html.includes("agent-workbench-trigger"));
    assert.ok(!html.toLowerCase().includes("workbench"));
  });

  test("every control is disabled while a submission is in flight", () => {
    const html = render({ value: "go", disabled: true });

    assert.equal(count(html, 'disabled=""'), 7, "4 chips + folder button + textarea + Start");
  });
});

describe("chat mode composer", () => {
  test("no agent controls leak into chat mode", () => {
    const html = render({ mode: "chatbot" });

    assert.ok(!html.includes("agent-context-pickers"));
    assert.ok(!html.includes('aria-label="Start Agent"'));
  });

  test("chat mode keeps the model picker", () => {
    const html = render({ mode: "chatbot" });

    assert.ok(html.includes('aria-label="Choose LLM"'));
    assert.ok(html.includes("Local / qwen3"));
  });
});

describe("dialog exits", () => {
  const dialog = (props = {}) =>
    renderToStaticMarkup(
      createElement(Modal, { title: "Coding Workbench", onClose() {}, ...props }, "body"),
    );

  test("a back affordance appears only when a label is given", () => {
    assert.ok(!dialog().includes("dialog-back"));
    assert.ok(dialog({ backLabel: "Back to composer" }).includes("dialog-back"));
  });

  test("the close control names its keyboard shortcut", () => {
    const html = dialog();

    assert.ok(html.includes('aria-label="Close dialog (Escape)"'));
    assert.ok(html.includes('title="Close (Esc)"'));
  });
});
