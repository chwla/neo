/**
 * The dictation meter's live numbers, kept outside React state.
 *
 * The input level arrives about 23 times a second and the clock four times. Held
 * as state in the hook, each tick re-rendered whatever owned the hook -- the
 * whole app -- to move one bar and one number. Here they live in a store only
 * the meter subscribes to (with `useSyncExternalStore`), so the rest of the app
 * renders when dictation changes phase, not when the room gets louder.
 *
 * What React requires of an external store, and this keeps:
 *  - `subscribe` and `getSnapshot` are the same functions for the store's life,
 *    so subscribing does not churn on every render;
 *  - `getSnapshot` returns the same frozen object until a value actually
 *    changes, so an unchanged store never reads as an update;
 *  - listeners are told only when something changed, and can unsubscribe.
 */
const ZERO = Object.freeze({ level: 0, seconds: 0 });

export function createMeter() {
  let snapshot = ZERO;
  const listeners = new Set();

  function set(partial) {
    const level = partial.level ?? snapshot.level;
    const seconds = partial.seconds ?? snapshot.seconds;
    if (level === snapshot.level && seconds === snapshot.seconds) return;
    snapshot = Object.freeze({ level, seconds });
    for (const listener of [...listeners]) listener();
  }

  return {
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    getSnapshot() {
      return snapshot;
    },
    set,
    reset() {
      set(ZERO);
    },
  };
}
