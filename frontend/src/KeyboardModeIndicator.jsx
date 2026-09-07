import { useEffect, useState } from "react";

import { usePendingChords } from "./keys/useCommands.js";
import { formatSequence } from "./keys/chord.js";
import { deriveMode, focusKind } from "./keys/mode.js";

/**
 * Which mode the keyboard is in, and -- when it is in the one where letters act
 * -- how to get out of it.
 *
 * The hint is the point. Mode is derived from focus rather than stored, so
 * nobody can be stuck in Command mode in any real sense, but somebody who
 * pressed a key and watched the page scroll instead of typing does not know
 * that. Saying "i to type" costs one line and is the difference between a
 * surprise and a mode.
 *
 * Focus is not React state, so it is watched directly. focusout fires before
 * the new element has focus, which is why the read is deferred by a tick --
 * reading activeElement during the event gives the body every time.
 */
export default function KeyboardModeIndicator({ enabled }) {
  const [mode, setMode] = useState("command");
  const pending = usePendingChords();

  useEffect(() => {
    if (!enabled || typeof document === "undefined") return undefined;

    let timer = null;
    function update() {
      setMode(deriveMode(focusKind(document.activeElement)));
    }
    function schedule() {
      clearTimeout(timer);
      timer = setTimeout(update, 0);
    }

    update();
    document.addEventListener("focusin", schedule);
    document.addEventListener("focusout", schedule);
    return () => {
      clearTimeout(timer);
      document.removeEventListener("focusin", schedule);
      document.removeEventListener("focusout", schedule);
    };
  }, [enabled]);

  if (!enabled) {
    return null;
  }

  const typing = mode === "typing";
  return (
    <div className={`kb-mode${typing ? " is-typing" : ""}`} role="status" aria-live="polite">
      <strong>{typing ? "TYPING" : "COMMAND"}</strong>
      {pending.length > 0 ? (
        <kbd className="kb-mode-pending">{formatSequence(pending)}</kbd>
      ) : (
        <small>{typing ? "Esc for commands" : "i to type"}</small>
      )}
    </div>
  );
}
