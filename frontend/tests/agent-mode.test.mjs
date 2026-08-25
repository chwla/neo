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
  test("offers repo, mode and agent as labelled chips", () => {
    const html = render();

    assert.equal(count(html, 'class="agent-chip"'), 3);
    for (const label of ["Repo", "Mode", "Agent"]) {
      assert.ok(
        html.includes(`<span class="agent-chip-label">${label}</span>`),
        `missing ${label} chip`,
      );
    }
  });

  // A run started from the composer is not filed under a project any more, so
  // the picker that used to say so is gone rather than left showing "No project".
  test("there is no project picker", () => {
    const html = render();

    assert.ok(!html.includes("No project"));
    assert.ok(!html.includes('<span class="agent-chip-label">Project</span>'));
  });

  // Only the objective is on show: everything that configures the run is one
  // click away behind the "+", and nothing of it is on the composer's face.
  test("repo, mode, agent and the clip live behind the + button", () => {
    const html = render();
    const menu = html.slice(html.indexOf('id="composer-menu"'));

    assert.ok(html.includes('aria-label="Open the composer menu"'), "the + trigger is missing");
    for (const control of [
      'aria-label="Select repository for agent"',
      'aria-label="Select permission mode"',
      'aria-label="Select agent definition"',
      'aria-label="Open a folder"',
    ]) {
      assert.ok(menu.includes(control), `${control} is not in the menu`);
    }
  });

  test("the menu starts closed", () => {
    const html = render();

    assert.ok(/id="composer-menu"[^>]*hidden/.test(html), "the menu must not be open on first paint");
  });

  test("the composer no longer has an expand caret", () => {
    for (const mode of ["agent", "chatbot"]) {
      const html = render({ mode });
      assert.ok(!html.includes("composer-expand"), `caret still in ${mode} mode`);
      assert.ok(!html.includes("Expand the composer"), `caret label still in ${mode} mode`);
    }
  });

  // The clip is the same affordance chat mode uses to attach files: in agent
  // mode what it attaches is a folder to work in.
  test("a folder on this computer can be opened from the clip", () => {
    const html = render();

    assert.ok(html.includes("agent-folder-button"), "the clip is missing");
    assert.ok(html.includes('aria-label="Open a folder"'));
    assert.ok(!html.includes("agent-chip-action"), "the old folder chip is gone");
    assert.ok(
      html.includes("edits it directly"),
      "it must say the agent edits the real folder, not a copy",
    );
  });

  // Agent runs take files the same way chat does: the menu offers both a folder
  // to work in and files to read.
  test("files can be attached in agent mode too", () => {
    const menu = render().slice(render().indexOf('id="composer-menu"'));

    assert.ok(menu.includes('aria-label="Attach files"'));
    assert.ok(menu.includes('aria-label="Open a folder"'));
  });

  test("the menu offers a Tools entry", () => {
    const html = render();

    assert.ok(html.includes("agent-tools-button"), "the Tools entry is missing");
    assert.ok(html.includes('aria-label="Tools"'));
  });

  test("the clip reports an attach in progress", () => {
    const html = render({ folderAttaching: true });

    assert.ok(html.includes('aria-label="Opening a folder"'), "an in-flight attach must be visible");
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

  // One control for both kinds of turn. The toggle above it decides what the
  // next one does; a separate "Start Agent" button would say that twice, and
  // would have to be reconciled with Chat's Send every time either changed.
  test("there is one send action, whichever kind of turn is next", () => {
    const html = render({ value: "add a docstring" });

    assert.ok(html.includes('aria-label="Send message"'));
    assert.ok(html.includes('class="neo-button send-button"'));
    assert.ok(!html.includes('aria-label="Start Agent"'), "agent mode is not a separate submit");
    assert.ok(!html.includes(">Plan</button>"), "the checklist step was removed");
    assert.ok(!html.includes("agent-plan-preview"));
    assert.ok(!html.includes("agent-created-tasks"));
  });

  test("send is disabled until there is something to send", () => {
    const blank = render({ value: "   ", selectedRepoId: "r1" });
    const typed = render({ value: "do the thing", selectedRepoId: "r1" });

    assert.ok(blank.includes('disabled="" aria-label="Send message"'));
    assert.ok(!typed.includes('disabled="" aria-label="Send message"'));
  });

  // Agent runs resolve their model through the same picker chat uses, so it has
  // to be readable and changeable without leaving agent mode.
  test("agent mode carries the model picker", () => {
    const html = render();

    assert.ok(html.includes('aria-label="Choose LLM"'));
    assert.ok(html.includes("Local / qwen3"));
  });

  // The composer says nothing on its own: the controls are visible and standing
  // advice under them is noise. What blocks a run is said by the disabled Start.
  test("no standing hint in any state", () => {
    for (const props of [{}, { selectedRepoId: "r1" }, { selectedRepoId: "r1", value: "do the thing" }]) {
      assert.ok(!render(props).includes("agent-mode-hint"));
    }
  });

  // A repository is no longer a precondition. Without one the registry withholds
  // the file and command tools and the run does what it still can, so blocking
  // the composer would make the Agent toggle dead in any conversation that has
  // not opened a folder -- which is most of them.
  test("no repository does not block sending", () => {
    const html = render({ selectedRepoId: "", value: "do the thing" });

    assert.ok(!html.includes("Select a workspace first"));
    assert.ok(!html.includes('title="Select a repository first"'));
    assert.ok(!html.includes('disabled="" aria-label="Send message"'));
    assert.ok(html.includes('aria-label="Open a folder"'), "attaching one stays reachable");
  });

  // Typing while a run is working is a correction, not a second turn, so the
  // composer has to say which of the two it is about to do.
  test("the placeholder says when typing will steer a run", () => {
    assert.ok(render({ steering: true }).includes("Steer the agent"));
    assert.ok(render().includes("What should the agent work on?"));
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

    assert.equal(count(html, 'disabled=""'), 10, "3 chips + folder + tools + attach + compact + model + textarea + Start");
  });
});

describe("chat mode composer", () => {
  test("no agent controls leak into chat mode", () => {
    const html = render({ mode: "chatbot" });

    assert.ok(!html.includes("agent-context-pickers"));
    assert.ok(!html.includes('aria-label="Start Agent"'));
    assert.ok(!html.includes("agent-folder-button"));
    assert.ok(!html.includes("agent-tools-button"));
    assert.ok(!html.includes('aria-label="Select permission mode"'));
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
