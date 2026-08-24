import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { formatCompactTokens, resolveContextWindow } from "./chatPresentation.js";

//: Same budget as MessageActionsMenu's flip -- this popover sits in the same row.
const MENU_SPACE = 210;

const RING_RADIUS = 7;
const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;

/** The ring alone -- how full the session's context window is, or an empty/dim ring when that's unknowable. */
function ContextRing({ pct }) {
  const clamped = Math.max(0, Math.min(100, pct ?? 0));
  const offset = RING_CIRCUMFERENCE * (1 - clamped / 100);
  return (
    <svg
      className={`context-window-ring${pct === null ? " is-unknown" : ""}`}
      width="16"
      height="16"
      viewBox="0 0 16 16"
      aria-hidden="true"
    >
      <circle className="context-window-ring-track" cx="8" cy="8" r={RING_RADIUS} fill="none" strokeWidth="2" />
      {pct === null ? null : (
        <circle
          className="context-window-ring-fill"
          cx="8"
          cy="8"
          r={RING_RADIUS}
          fill="none"
          strokeWidth="2"
          strokeLinecap="round"
          strokeDasharray={RING_CIRCUMFERENCE}
          strokeDashoffset={offset}
          transform="rotate(-90 8 8)"
        />
      )}
    </svg>
  );
}

/**
 * How full the session's context window is, as a ring next to the reply --
 * click it for the full breakdown. Deliberately separate from the plain
 * "N tokens" text elsewhere in the footer: that's what *this* response cost,
 * this ring is what the *whole session* has used against the model's window.
 *
 * Same open/close/flip mechanics as MessageActionsMenu.jsx (and RowActionsMenu
 * in App.jsx) -- copied rather than shared, matching how those two already
 * duplicate the pattern instead of factoring it out.
 */
export function ContextWindowIndicator({ message, contextWindowIndex, sessionTokensUsed }) {
  const [open, setOpen] = useState(false);
  const [dropUp, setDropUp] = useState(false);
  const menuRef = useRef(null);
  const buttonRef = useRef(null);

  useLayoutEffect(() => {
    if (!open) {
      return;
    }
    const rect = buttonRef.current?.getBoundingClientRect();
    setDropUp(Boolean(rect) && window.innerHeight - rect.bottom < MENU_SPACE);
  }, [open]);

  useEffect(() => {
    if (!open) {
      return undefined;
    }

    function onPointerDown(event) {
      if (menuRef.current?.contains(event.target) || buttonRef.current?.contains(event.target)) {
        return;
      }
      setOpen(false);
    }

    function onKeyDown(event) {
      if (event.key === "Escape") {
        setOpen(false);
        buttonRef.current?.focus();
      }
    }

    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  // Gates on this message actually having gone through a real generation, not
  // on the token count shown -- that's `sessionTokensUsed`, a session-wide
  // figure the same on every message.
  if (!Number.isFinite(message.total_tokens)) {
    return null;
  }

  const used = Number.isFinite(sessionTokensUsed) ? sessionTokensUsed : 0;
  const windowSize = resolveContextWindow(message, contextWindowIndex);
  // Not clamped -- a conversation that ran past the window should read over 100%,
  // not pin at it. Only the ring/bar's own fill is clamped, further down.
  const pct = windowSize ? (used / windowSize) * 100 : null;

  return (
    <span className="context-window">
      <button
        ref={buttonRef}
        type="button"
        className={`context-window-trigger${open ? " is-open" : ""}`}
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-label="Context window usage"
        title="Context window usage"
        onClick={() => setOpen((current) => !current)}
      >
        <ContextRing pct={pct} />
        <span className="context-window-pct-label">{pct === null ? "—" : `${Math.round(pct)}%`}</span>
      </button>
      <span
        ref={menuRef}
        className={`context-window-menu${dropUp ? " drop-up" : ""}`}
        hidden={!open}
        role="dialog"
        aria-label="Context window usage"
      >
        <strong className="context-window-title">Context Window</strong>
        {windowSize ? (
          <>
            <span className="ws-progress context-window-bar">
              <span style={{ width: `${Math.min(100, pct)}%` }} />
            </span>
            <span className="context-window-totals">
              {formatCompactTokens(used)} used <span>{formatCompactTokens(windowSize)} total</span>
            </span>
            <span className="context-window-fact-row">
              <span>Model</span>
              <span>{message.model_name || "—"}</span>
            </span>
            <span className="context-window-fact-row">
              <span>Usage</span>
              <span>{pct.toFixed(1)}%</span>
            </span>
            <span className="context-window-fact-row">
              <span>Window</span>
              <span>{formatCompactTokens(windowSize)} tokens</span>
            </span>
          </>
        ) : (
          <>
            <span className="context-window-totals">{formatCompactTokens(used)} tokens used</span>
            <span className="context-window-fact-row">
              <span>Model</span>
              <span>{message.model_name || "—"}</span>
            </span>
            <span className="context-window-fact-row">
              <span>Maximum context</span>
              <span>Unknown</span>
            </span>
            <p className="context-window-note">
              Neo can measure the tokens currently used, but this model has no reliably known
              maximum context size. Percentage usage is therefore unavailable.
            </p>
          </>
        )}
      </span>
    </span>
  );
}
