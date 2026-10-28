import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import PasswordField from "../PasswordField.jsx";
import Icon from "../WorkspaceIcon.jsx";
import { registerModal } from "../modalStack.js";
import { useVisibleArea } from "./ui.jsx";

/**
 * What a locked note shows while its key is closed: its title (titles stay
 * visible, as in Apple Notes) and a way to open it. Nothing else of it is here.
 */
export function LockedNote({ controller, session, lock, toolbarStart, toolbarEnd }) {
  const reason = !lock.available
    ? "Locked notes can only be opened over a secure connection (HTTPS, or Neo on this computer)."
    : lock.guest
      ? "Guest sessions have no password, so they can't open locked notes."
      : null;
  return (
    <div className="nw-editor">
      <div className="nw-toolbar" role="toolbar" aria-label="Note">
        {toolbarStart}
        <div className="nw-tool-group nw-nav-tool">{toolbarEnd}</div>
      </div>
      <div className="nw-locked" role="region" aria-label="Locked note">
        <span className="nw-locked-icon" aria-hidden="true">
          <Icon name="lock" />
        </span>
        <h2>{session.note?.title || "Locked Note"}</h2>
        <p>This note is locked.</p>
        {reason ? (
          <p className="nw-locked-why">{reason}</p>
        ) : (
          <button type="button" className="nw-locked-open" onClick={() => controller.openLocked()} disabled={Boolean(lock.busy)}>
            View Note
          </button>
        )}
      </div>
    </div>
  );
}

const PROMPTS = {
  setup: {
    title: "Lock Note",
    message: "Locked notes use your profile password. If you forget it, Neo can't open them, and they can't be recovered.",
    confirm: "Lock Note",
  },
  unlock: {
    title: "Enter Password",
    message: "Enter your profile password to view locked notes.",
    confirm: "OK",
  },
  earlier: {
    title: "Earlier Password",
    message: "This note was locked with an earlier password. Enter that password to open it. It is then opened with your current password from now on.",
    confirm: "OK",
  },
};

/** The profile password, asked for to set up, open or recover locked notes. */
export function LockPrompt({ controller, prompt }) {
  const [password, setPassword] = useState("");
  const [earlier, setEarlier] = useState("");
  const input = useRef(null);
  const scrim = useRef(null);
  useVisibleArea(scrim);
  const text = PROMPTS[prompt.mode] ?? PROMPTS.unlock;

  useEffect(() => {
    const unregister = registerModal(() => controller.cancelLockPrompt());
    input.current?.focus();
    return unregister;
  }, [controller]);

  // A wrong password clears the field for another try, as sign-in does.
  useEffect(() => {
    if (prompt.error) {
      setPassword("");
      setEarlier("");
      input.current?.focus();
    }
  }, [prompt.error]);

  const ready = password && (prompt.mode !== "earlier" || earlier);
  return createPortal(
    <div ref={scrim} className="nw-scrim" onPointerDown={(event) => event.target === event.currentTarget && controller.cancelLockPrompt()}>
      <form
        className="nw-dialog nw-lock-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="nw-lock-title"
        aria-describedby="nw-lock-message"
        onSubmit={(event) => {
          event.preventDefault();
          if (ready && !prompt.busy) controller.submitLockPassword(password, prompt.mode === "earlier" ? earlier : null);
        }}
      >
        <span className="nw-lock-dialog-icon" aria-hidden="true">
          <Icon name="lock" />
        </span>
        <h2 id="nw-lock-title">{text.title}</h2>
        <p id="nw-lock-message">{text.message}</p>
        <PasswordField
          label={prompt.mode === "earlier" ? "Current profile password" : "Profile password"}
          value={password}
          onChange={setPassword}
          inputRef={input}
          disabled={prompt.busy}
          required
        />
        {prompt.mode === "earlier" && (
          <PasswordField label="Earlier password" value={earlier} onChange={setEarlier} autoComplete="off" disabled={prompt.busy} required />
        )}
        {prompt.error && (
          <p className="nw-lock-error" role="alert">
            {prompt.error}
          </p>
        )}
        <div className="nw-dialog-actions">
          <button type="button" onClick={() => controller.cancelLockPrompt()} disabled={prompt.busy}>
            Cancel
          </button>
          <button type="submit" className="primary" disabled={!ready || prompt.busy} aria-busy={Boolean(prompt.busy)}>
            {prompt.busy ? "Opening…" : text.confirm}
          </button>
        </div>
      </form>
    </div>,
    document.body,
  );
}
