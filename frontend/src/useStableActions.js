import { useLayoutEffect, useRef, useState } from "react";

// Event handlers for a memoized subtree. Keys are fixed for the hook's lifetime;
// callers always invoke the latest committed handler, never a render in flight.
export function useStableActions(handlers) {
  const current = useRef(handlers);
  useLayoutEffect(() => { current.current = handlers; });
  const [actions] = useState(() => Object.fromEntries(
    Object.keys(handlers).map((name) => [name, (...args) => current.current[name](...args)]),
  ));
  return actions;
}
