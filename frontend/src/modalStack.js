/**
 * Escape routing for open dialogs.
 *
 * Dialogs stack: a confirmation can open on top of a settings dialog. Escape has
 * to reach the top-most one only, or a single keypress dismisses the whole pile.
 * The stack lives here rather than inside the component so the ordering rule is
 * testable on its own, and so the window listener is attached once for the whole
 * stack instead of once per open dialog.
 */

const stack = [];

//: Dialogs that route Escape are a subset of dialogs that cover the app: four
//: of the settings panels hand-roll their own backdrop and have never been in
//: the stack. They still cover the field, so coverage is counted separately
//: rather than inferred from the stack's depth.
let covers = 0;

/**
 * Mirror "something is over the app" onto the document element.
 *
 * It lives here because dialogs nest, and the flag is about whether the app is
 * covered at all rather than by how many things -- which makes it a count with
 * one edge that matters, in the module that already owns what is open.
 *
 * What reads it is the background engine, and the reason is cost rather than
 * taste. The field repaints every frame, a `backdrop-filter` re-runs whenever
 * anything beneath it changes, and the settings dialog is glass -- so an open
 * dialog turns an ambient animation nobody can see into a full blur pass at
 * 60fps. Stopping the field while it is covered makes that filter free instead
 * of merely cheap. The stylesheet uses the same flag to park the wash.
 *
 * Written as an attribute on `document.documentElement`, next to `data-theme`
 * and `data-chat-bg`, so CSS and the engine can both see it without either of
 * them being wired to this module.
 */
function syncCoveredFlag() {
  if (typeof document === "undefined" || !document.documentElement) {
    return;
  }

  if (covers > 0) {
    document.documentElement.dataset.modalOpen = "";
  } else {
    delete document.documentElement.dataset.modalOpen;
  }
}

/**
 * Declare that a dialog is covering the app, and nothing else.
 *
 * Separate from `registerModal` on purpose. Every dialog covers the field, but
 * only the ones built on `Modal` route Escape, and giving the other four that
 * as a side effect of a performance fix would close forms on a keypress that
 * never closed them before. So this is the half they need and none of the half
 * they do not. Returns its own release, safe to call more than once.
 */
export function registerCover() {
  covers += 1;
  syncCoveredFlag();

  let released = false;
  return function uncover() {
    if (released) {
      return;
    }

    released = true;
    covers -= 1;
    syncCoveredFlag();
  };
}

/** Number of dialogs currently registered. Exposed for tests. */
export function openModalCount() {
  return stack.length;
}

/**
 * Routes an Escape keydown to the top-most dialog. Returns whether it was
 * handled, so callers can tell "no dialog was open" from "a dialog closed".
 */
export function dispatchEscape(event) {
  if (event?.key !== "Escape" || stack.length === 0) {
    return false;
  }

  stack[stack.length - 1].onEscape?.();
  return true;
}

/**
 * Registers a dialog as the new top of the stack. Returns its unregister
 * function, which is safe to call more than once.
 */
export function registerModal(onEscape) {
  const entry = { onEscape };
  stack.push(entry);
  const uncover = registerCover();

  if (stack.length === 1 && typeof window !== "undefined") {
    window.addEventListener("keydown", dispatchEscape);
  }

  return function unregister() {
    const index = stack.indexOf(entry);
    if (index < 0) {
      return;
    }

    stack.splice(index, 1);
    uncover();
    if (stack.length === 0 && typeof window !== "undefined") {
      window.removeEventListener("keydown", dispatchEscape);
    }
  };
}

/** Drops every registration. Test-only; nothing in the app unwinds the stack. */
export function resetModalStack() {
  stack.length = 0;
  covers = 0;
  syncCoveredFlag();
  if (typeof window !== "undefined") {
    window.removeEventListener("keydown", dispatchEscape);
  }
}
