/**
 * The React side of the keyboard: three hooks, no context.
 *
 * `useCommandHandlers` is the one with a trick in it. The functions it is given
 * are declared inside a component body, so their identity changes every render --
 * registering them directly would tear the whole map down and build it up again
 * sixty times a second. Instead the map is held in a ref that is refreshed on
 * every render, and what gets registered once, on mount, is a stable shim that
 * reads through the ref. This is the same device the Modal component already uses
 * to keep an inline onClose from shuffling the dialog stack.
 */

import { useEffect, useMemo, useRef, useState } from "react";

import { armEngine, onPendingChange } from "./engine.js";
import { registerCommandHandlers } from "./registry.js";

/**
 * Points command ids at this component's handlers for as long as it is mounted.
 *
 * The map may be written inline at the call site; it is not a dependency. Only
 * the set of ids is, so adding or removing a command re-registers and changing a
 * closure does not.
 */
export function useCommandHandlers(map) {
  const latest = useRef(map);
  latest.current = map;

  const ids = Object.keys(map ?? {}).sort().join("|");

  useEffect(() => {
    const shims = {};
    for (const id of ids ? ids.split("|") : []) {
      shims[id] = (context) => latest.current?.[id]?.(context);
    }
    return registerCommandHandlers(shims);
  }, [ids]);
}

/**
 * Attaches the keyboard once, for the life of the app.
 *
 * Both arguments are read per keystroke rather than captured, so a keymap rebuilt
 * when the profile's settings arrive takes effect without the listener being
 * detached -- which matters because detaching and reattaching mid-keystroke is
 * how you lose one.
 */
export function useKeyboardEngine(keymap, scopes) {
  const keymapRef = useRef(keymap);
  keymapRef.current = keymap;
  const scopesRef = useRef(scopes);
  scopesRef.current = scopes;

  useEffect(() => armEngine({
    getKeymap: () => keymapRef.current,
    getContext: () => ({ scopes: scopesRef.current }),
  }), []);
}

/** The chords typed so far towards a longer binding, for the pending indicator. */
export function usePendingChords() {
  const [pending, setPending] = useState([]);
  useEffect(() => onPendingChange(setPending), []);
  return pending;
}

/**
 * The scope tokens that hold right now, as a Set the dispatcher can test against.
 * Memoised on the values themselves so a keystroke does not rebuild it.
 */
export function useScopes(activeView, extras = []) {
  const flags = extras.join("|");
  return useMemo(
    () => new Set([activeView, ...(flags ? flags.split("|") : [])]),
    [activeView, flags],
  );
}
