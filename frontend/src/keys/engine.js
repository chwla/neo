/**
 * The one window listener, and nothing else.
 *
 * Every decision this makes is delegated: what a keystroke means to
 * dispatcher.js, what has focus to mode.js, what to run to registry.js. What is
 * left here is the part that cannot be tested without a DOM -- attaching the
 * listener, holding the sequence timeout, and the suspend flag -- which is why it
 * is kept this small.
 *
 * Two rules of precedence are worth reading before anything else.
 *
 * An event somebody nearer already handled is skipped. The composer's Enter, the
 * Tab that inserts two spaces in a note and Research's mod+Enter all call
 * preventDefault, so local handlers keep winning without either side knowing the
 * other exists.
 *
 * While any dialog is open the engine is inert. modalStack.js already runs its
 * own listener for Escape, and two listeners both claiming Escape is how a single
 * keypress ends up closing two things. Deferring wholesale is the version with no
 * ordering puzzle in it -- the cost is that no shortcut works over a dialog,
 * which is deliberate for now.
 */

import { openModalCount } from "../modalStack.js";
import { createDispatcher } from "./dispatcher.js";
import { focusKind } from "./mode.js";
import { runCommand } from "./registry.js";

/** How long a half-typed sequence waits for its next chord. */
export const SEQUENCE_TIMEOUT_MS = 900;

let armed = null;
let suspended = false;
const pendingListeners = new Set();

/** Which modifier `mod` means here. Read once, at arm time. */
export function detectPlatform() {
  if (typeof navigator === "undefined") {
    return "other";
  }
  const source = navigator.userAgentData?.platform || navigator.platform || navigator.userAgent || "";
  return /mac|iphone|ipad|ipod/i.test(source) ? "mac" : "other";
}

function announcePending(chords) {
  for (const listener of pendingListeners) {
    listener(chords);
  }
}

/**
 * Called whenever the pending sequence changes, so an indicator can show what is
 * half-typed. Returns its own unsubscribe.
 */
export function onPendingChange(listener) {
  pendingListeners.add(listener);
  return () => pendingListeners.delete(listener);
}

/**
 * Attaches the keyboard for the life of the app.
 *
 * `getKeymap` and `getContext` are read per keystroke rather than captured, so a
 * keymap rebuilt when the profile's settings arrive, or when Command mode is
 * switched on, takes effect without the listener being detached and reattached.
 */
export function armEngine(config = {}) {
  const {
    getKeymap,
    getContext = () => ({ scopes: new Set() }),
    timeoutMs = SEQUENCE_TIMEOUT_MS,
  } = config;

  if (typeof window === "undefined") {
    return () => {};
  }

  let built = null;
  let timer = null;

  function dispatcherNow() {
    const keymap = getKeymap();
    // Rebuilt only when the keymap itself is a different object, which is exactly
    // when React's useMemo hands over a new one.
    if (!built || built.keymap !== keymap) {
      built = { keymap, dispatcher: createDispatcher({ keymap, commandMode: keymap.commandMode }) };
      announcePending([]);
    }
    return built.dispatcher;
  }

  function cancelTimer() {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  }

  function onKeyDown(event) {
    if (suspended || event.defaultPrevented) {
      return;
    }
    // A dialog is up. modalStack owns the keyboard until it closes.
    if (openModalCount() > 0) {
      return;
    }

    const dispatcher = dispatcherNow();
    const target = event.target ?? null;
    const kind = focusKind(target);

    // Escape is handled here rather than through the keymap, because it has to
    // keep working when the keymap has nothing to say -- it is the way out of a
    // text field, and the way to abandon a half-typed sequence.
    if (event.key === "Escape") {
      cancelTimer();
      dispatcher.reset();
      announcePending([]);
      if (kind === "text" && typeof target?.blur === "function") {
        event.preventDefault();
        target.blur();
      }
      return;
    }

    const context = { ...getContext(), focusKind: kind };
    const result = dispatcher.feed(event, context);

    if (result.action === "pending") {
      cancelTimer();
      timer = setTimeout(() => {
        timer = null;
        dispatcher.expire();
        announcePending([]);
      }, timeoutMs);
      announcePending(dispatcher.pending);
      if (result.preventDefault) {
        event.preventDefault();
      }
      return;
    }

    cancelTimer();
    announcePending([]);

    if (result.action !== "run") {
      return;
    }
    // The handler runs first. One that declines -- nothing to stop, no transcript
    // to scroll -- leaves the keystroke to the browser rather than eating it.
    const acted = runCommand(result.commandId, {
      count: result.count,
      sequence: result.sequence,
      event,
    });
    if (acted && result.preventDefault) {
      event.preventDefault();
      event.stopPropagation();
    }
  }

  window.addEventListener("keydown", onKeyDown);
  armed = { onKeyDown, cancelTimer };

  return function disarm() {
    cancelTimer();
    window.removeEventListener("keydown", onKeyDown);
    if (armed?.onKeyDown === onKeyDown) {
      armed = null;
    }
  };
}

/**
 * Stops the engine acting without detaching it.
 *
 * The reason this exists is the settings screen: recording "g c" as a new binding
 * while the engine is live navigates you to the chat view halfway through the
 * recording. The capture field suspends on focus and resumes on blur.
 */
export function suspendEngine() {
  suspended = true;
  armed?.cancelTimer?.();
  announcePending([]);
}

export function resumeEngine() {
  suspended = false;
}

export function isEngineSuspended() {
  return suspended;
}

/** Whether a listener is currently attached. Exposed for tests. */
export function isEngineArmed() {
  return armed !== null;
}

/** Drops every listener and subscriber. Test-only; the app never unwinds this. */
export function resetEngine() {
  suspended = false;
  pendingListeners.clear();
  if (armed && typeof window !== "undefined") {
    window.removeEventListener("keydown", armed.onKeyDown);
  }
  armed = null;
}
