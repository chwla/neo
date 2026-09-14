import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { flushSync } from "react-dom";
import { ChatComposer, ChatMessage, ChatTranscript } from "../src/App.jsx";
import { useStableActions } from "../src/useStableActions.js";
import "../src/index.css";

// Exercise actual React components in a browser, including memo bailouts and
// event-handler freshness, which server-rendered markup tests cannot measure.
let reads = 0;
const messages = Array.from({ length: 500 }, (_, i) => ({
  id: i + 1, role: i % 2 ? "assistant" : "user", created_at: "2026-09-14T12:00:00Z",
  get content() { reads++; return `Message ${i + 1}: **formatted text** with a list.\n\n- One\n- Two`; },
}));
const contextWindowIndex = {};
const agentRuns = {};
let update;
let lastAction;
let lastActions;
const root = createRoot(document.getElementById("root"));

function Fixture({ optimized }) {
  const [version, setVersion] = useState(0);
  update = setVersion;
  const actions = useStableActions({
    onCopy: (text) => { lastAction = { text, version }; },
    onRerun: (text) => { lastAction = { text, version }; },
  });
  lastActions = actions;
  const props = {
    messages, contextWindowIndex, sessionTokensUsed: 0, agentRuns,
    editingMessageId: null, editingValue: "", openThinkingMessageId: null,
    agentBusy: false, agentPatch: "", agentPatchSessionId: null, actions,
  };
  return <><output>{version}</output>{optimized
    ? <ChatTranscript {...props} />
    : messages.map((message) => <ChatMessage key={message.id} message={message}
      messages={messages} contextWindowIndex={contextWindowIndex} sessionTokensUsed={0} {...actions} />)
  }</>;
}

let editComposer;
function ComposerFixture() {
  const [value, setValue] = useState("");
  editComposer = setValue;
  return <ChatComposer value={value} onChange={setValue} mode="chatbot"
    llms={[]} agentDefinitions={[]} onSubmit={(event) => event.preventDefault()} />;
}

window.__benchRun = async () => {
  const runs = [];
  for (const optimized of [false, true]) {
    flushSync(() => root.render(<Fixture key={String(optimized)} optimized={optimized} />));
    const stable = lastActions;
    reads = 0;
    const start = performance.now();
    for (let i = 1; i <= 50; i++) flushSync(() => update(i));
    const ms = performance.now() - start;
    const contentReads = reads;
    if (lastActions !== stable) throw new Error("action identity changed");
    lastActions.onCopy("latest");
    if (lastAction.version !== 50) throw new Error("memoized actions used a stale render");
    if (optimized && contentReads !== 0) throw new Error("unchanged saved messages rerendered");
    if (document.querySelectorAll(".neo-chat-message").length !== 500) throw new Error("transcript lost rows");
    // Click the real per-message menu actions after all parent updates. The
    // stored row still needs the newest callback and its nearest user prompt.
    const reply = document.querySelectorAll(".neo-chat-message")[3];
    const rerun = [...reply.querySelectorAll("button")].find((button) => button.textContent === "Rerun");
    flushSync(() => rerun.click());
    if (lastAction.version !== 50 || !lastAction.text.startsWith("Message 3:")) throw new Error("Rerun lost its preceding prompt or latest handler");
    runs.push({ optimized, messages: 500, parentUpdates: 50, ms, contentReads });
  }
  flushSync(() => root.render(<ComposerFixture />));
  const textarea = document.querySelector("textarea");
  const shortHeight = textarea.clientHeight;
  flushSync(() => editComposer("A full line of text.\n".repeat(40)));
  const tallHeight = textarea.clientHeight;
  if (tallHeight <= shortHeight || textarea.style.overflowY !== "auto") throw new Error("composer did not grow and bound its content");
  flushSync(() => editComposer("Short again"));
  if (textarea.clientHeight !== shortHeight || textarea.style.overflowY !== "hidden") throw new Error("composer did not shrink after edit");
  return { runs, checks: "500 rows retained; no saved-message reads during parent updates; latest committed menu actions and preceding prompt; composer grows, bounds and shrinks" };
};
window.__benchReady = true;
