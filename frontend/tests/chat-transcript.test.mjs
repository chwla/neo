import assert from "node:assert/strict";
import { test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ChatMessage, ChatTranscript } from "../src/App.jsx";

const messages = [
  { id: 1, role: "assistant", content: "No preceding prompt" },
  { id: 2, role: "user", content: "First prompt" },
  { id: 3, role: "assistant", content: "Answer", thinking: "Reasoning" },
  { id: 4, role: "user", content: "Second prompt" },
  { id: 5, role: "assistant", content: "Another answer" },
];

for (const mode of ["reading", "editing", "thinking"]) {
  test(`memoized transcript preserves message markup and actions while ${mode}`, () => {
    const props = {
      messages, contextWindowIndex: {}, sessionTokensUsed: 0,
      editingMessageId: mode === "editing" ? 4 : null,
      editingValue: mode === "editing" ? "Revised prompt" : "",
      openThinkingMessageId: mode === "thinking" ? 3 : null,
      agentRuns: {}, agentBusy: false, agentPatch: "", agentPatchSessionId: null,
      actions: { onSetEditingValue() {} },
    };
    const actual = renderToStaticMarkup(createElement(ChatTranscript, props));
    const expected = messages.map((message) => renderToStaticMarkup(createElement(ChatMessage, {
      ...props, ...props.actions, message, thinkingOpen: props.openThinkingMessageId === message.id,
    }))).join("");
    assert.equal(actual, expected);
    assert.equal((actual.match(/disabled=""/g) || []).length, 1,
      "only the reply with no preceding prompt should disable Rerun");
  });
}
