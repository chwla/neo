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

  if (stack.length === 1 && typeof window !== "undefined") {
    window.addEventListener("keydown", dispatchEscape);
  }

  return function unregister() {
    const index = stack.indexOf(entry);
    if (index < 0) {
      return;
    }

    stack.splice(index, 1);
    if (stack.length === 0 && typeof window !== "undefined") {
      window.removeEventListener("keydown", dispatchEscape);
    }
  };
}

/** Drops every registration. Test-only; nothing in the app unwinds the stack. */
export function resetModalStack() {
  stack.length = 0;
  if (typeof window !== "undefined") {
    window.removeEventListener("keydown", dispatchEscape);
  }
}
