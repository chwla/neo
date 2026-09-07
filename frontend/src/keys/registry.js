/**
 * Where a command id meets the function that carries it out.
 *
 * The registry is module-level rather than a React context, for the same reason
 * modalStack.js is: the consumer is a window listener, which is not in the React
 * tree, and a context would exist only so that something outside the tree could
 * read it.
 *
 * The useful consequence is that a handler is registered by whichever component
 * already owns the state. The sidebar's search box, Notes' editor and Gallery's
 * filter each register their own command and keep their useState exactly where it
 * is -- none of them had to be lifted into NeoApp to become reachable from the
 * keyboard.
 *
 * Registrations stack per id. Two components can claim the same command across a
 * remount, the most recent wins, and an unmount removes its own entry rather than
 * whatever happens to be there -- which is what stops React's mount/unmount order
 * during a fast refresh from leaving a command pointing at a dead closure.
 */

const stacks = new Map();

/**
 * Points a set of command ids at handlers, and returns the function that takes
 * them away again. Safe to call more than once, like modalStack's unregister.
 *
 * A handler may return false to decline -- "stop generating" when nothing is
 * generating -- which leaves the keystroke to the browser instead of swallowing it.
 */
export function registerCommandHandlers(map) {
  const entries = Object.entries(map ?? {})
    .filter(([, handler]) => typeof handler === "function")
    .map(([id, handler]) => ({ id, handler }));

  for (const entry of entries) {
    const stack = stacks.get(entry.id);
    if (stack) stack.push(entry);
    else stacks.set(entry.id, [entry]);
  }

  let released = false;
  return function unregister() {
    if (released) {
      return;
    }
    released = true;
    for (const entry of entries) {
      const stack = stacks.get(entry.id);
      if (!stack) continue;
      const index = stack.indexOf(entry);
      if (index >= 0) stack.splice(index, 1);
      if (stack.length === 0) stacks.delete(entry.id);
    }
  };
}

/**
 * Runs a command. An id nobody has claimed is a no-op rather than a throw: the
 * catalogue is allowed to name commands whose screen is not mounted, and a
 * profile's stored overrides are allowed to name commands that no longer exist.
 *
 * Returns false when the command declined or was not there, which the engine
 * reads as "leave the keystroke alone".
 */
export function runCommand(id, context = {}) {
  const stack = stacks.get(id);
  const entry = stack?.[stack.length - 1];
  if (!entry) {
    return false;
  }
  return entry.handler(context) !== false;
}

/** Whether anything can currently carry this command out. Used to grey palette rows. */
export function hasHandler(id) {
  return (stacks.get(id)?.length ?? 0) > 0;
}

/** Every id with a handler right now. Test and palette use only. */
export function registeredCommandIds() {
  return [...stacks.keys()];
}

/** Drops every registration. Test-only; nothing in the app unwinds the registry. */
export function resetCommandRegistry() {
  stacks.clear();
}
