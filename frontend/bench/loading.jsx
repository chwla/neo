import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { flushSync } from "react-dom";
import { lazyPanel } from "../src/lazyPanel.jsx";
import { applyEvents, IDLE, reduce } from "../src/chatStream.js";
import "../src/index.css";

let finishLoading;
let open;
let loads = 0;
const ThemePanel = lazyPanel(() => {
  loads++;
  return new Promise((resolve) => { finishLoading = resolve; });
});
function Fixture() {
  const [show, setShow] = useState(false);
  open = setShow;
  return <main><textarea defaultValue="An unsent draft" />
    {show && <ThemePanel theme="default" onThemeChange={() => {}} onClose={() => setShow(false)} />}
  </main>;
}
const root = createRoot(document.getElementById("root"));
const pause = () => new Promise((resolve) => requestAnimationFrame(resolve));
window.__benchRun = async () => {
  flushSync(() => root.render(<Fixture />));
  if (loads) throw new Error("Unused panel downloaded eagerly");
  const textarea = document.querySelector("textarea");
  textarea.value = "Draft survives loading";
  flushSync(() => open(true));
  if (!document.querySelector('[role="status"]')) throw new Error("No loading feedback");
  if (document.querySelector("textarea") !== textarea) throw new Error("Draft unmounted");
  finishLoading(await import("../src/AppearanceSettings.jsx"));
  for (let i = 0; i < 120 && !document.querySelector(".theme-card"); i++) await pause();
  if (!document.querySelector(".theme-card")) throw new Error("Theme panel never appeared");
  flushSync(() => open(false));
  flushSync(() => open(true));
  if (loads !== 1 || textarea.value !== "Draft survives loading") throw new Error("Panel cache or draft lost");
  const initial = new Map(Array.from({ length: 200 }, (_, i) => [i + 1, IDLE]));
  const events = Array.from({ length: 2000 }, (_, i) => ({
    chat_id: i % 200 + 1, type: "chunk", generation_id: `g${i % 200}`, content: "word ",
  }));
  const reference = (state) => events.reduce((current, event) => {
    const next = new Map(current);
    next.set(event.chat_id, reduce(current.get(event.chat_id) ?? IDLE, event));
    return next;
  }, state);
  const timings = [];
  for (let trial = 0; trial < 6; trial++) {
    for (const batched of trial % 2 ? [true, false] : [false, true]) {
      const started = performance.now();
      const result = batched ? applyEvents(initial, events) : reference(initial);
      const ms = performance.now() - started;
      if ([...result.values()].some((value) => value.text !== "word ".repeat(10))) throw new Error("Replay changed text");
      timings.push({ trial, batched, ms });
    }
  }
  return { checks: "lazy panel defers loading, shows feedback, preserves draft and reuses module; replay retains every token", chats: 200, events: 2000, timings };
};
window.__benchReady = true;
