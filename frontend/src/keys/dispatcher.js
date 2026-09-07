/**
 * What a keystroke means, decided without touching the DOM or a timer.
 *
 * The engine owns the window listener and the pending timeout; everything it
 * needs to decide lives here as a pure function of (event, context, buffer), so
 * the interesting behaviour -- sequences, counts, scope, the guard that stops a
 * bare key acting mid-sentence -- is tested by handing it plain objects. The
 * suite has no DOM and cannot dispatch a keypress, so anything that ends up in
 * the listener instead of here is effectively untested.
 *
 * `expire` is the timeout, as a method. The engine calls it when 900ms passes;
 * a test calls it directly and never needs a fake clock.
 */

import { chordFromEvent } from "./chord.js";
import { matchSequence } from "./keymap.js";
import { allowsBareKeys } from "./mode.js";

/** More than this and a repeat is a mistake rather than an instruction. */
export const MAX_COUNT = 999;

/** Nothing happened; the browser keeps the keystroke. */
const NOTHING = { action: "none", commandId: null, count: 1, sequence: "", preventDefault: false };

export function createDispatcher(options = {}) {
  const { keymap, commandMode = false } = options;
  let pending = [];
  let count = "";

  function clear() {
    pending = [];
    count = "";
  }

  function ran(binding) {
    // A count is an instruction to repeat, so a command that cannot repeat gets
    // one rather than silently doing something 30 times.
    const repeats = binding.repeatable ? Math.min(Number(count) || 1, MAX_COUNT) : 1;
    const sequence = binding.key;
    clear();
    return { action: "run", commandId: binding.id, count: repeats, sequence, preventDefault: true };
  }

  return {
    /**
     * What this keydown means. `context` is `{ scopes, focusKind }` -- the active
     * scope tokens, and what has focus.
     *
     * The returned `preventDefault` is advice, not a decision: the engine calls
     * the handler first, and a handler that declines by returning false leaves
     * the keystroke to the browser.
     */
    feed(event, context = {}) {
      const { scopes = new Set(), focusKind: kind = "none" } = context;

      // Somebody nearer the element already acted on this. The composer's Enter,
      // Notes' Tab-inserts-spaces and Research's mod+Enter all call
      // preventDefault, so this one line is the whole of how local handlers keep
      // winning without either side knowing about the other.
      if (event?.defaultPrevented) {
        return NOTHING;
      }

      const chord = chordFromEvent(event);
      if (chord === null) {
        return NOTHING;
      }

      const hard = Boolean(event.ctrlKey || event.altKey || event.metaKey);
      if (!hard && !allowsBareKeys(kind)) {
        // Someone is typing. Drop any half-finished sequence with it, so that
        // returning to the page later does not resume a buffer from minutes ago.
        clear();
        return NOTHING;
      }

      // A count only makes sense before a command, and only in Command mode. A
      // leading zero is not a count -- it is left free to be bound to something.
      if (commandMode && !hard && pending.length === 0
        && /^[0-9]$/.test(chord) && !(chord === "0" && count === "")) {
        count = (count + chord).slice(0, String(MAX_COUNT).length);
        return { action: "pending", commandId: null, count: 1, sequence: "", preventDefault: true };
      }

      const attempt = [...pending, chord];
      const result = matchSequence(keymap, attempt, scopes);

      if (result.status === "run") {
        return ran(result.binding);
      }
      if (result.status === "pending") {
        pending = attempt;
        // Held back so the browser's find-as-you-type does not open on the "g"
        // of "g c". Only when nothing has focus -- a modifier chord that happens
        // to be a prefix has no default worth blocking.
        return {
          action: "pending",
          commandId: null,
          count: 1,
          sequence: attempt.join(" "),
          preventDefault: allowsBareKeys(kind),
        };
      }
      clear();
      return NOTHING;
    },

    /**
     * The pending sequence timed out, and is dropped without a trace.
     *
     * There is deliberately no "run the prefix on its own" case here. A chord
     * that is a complete binding runs the moment it arrives rather than waiting
     * to see whether a longer one is coming, because the alternative is a visible
     * pause on every key that happens to start something else. The cost is that
     * binding "d" makes "d d" unreachable -- which is real, and is why
     * findConflicts reports exactly that as a shadow, and why the settings screen
     * says so while the key is being recorded rather than after.
     */
    expire() {
      clear();
      return NOTHING;
    },

    /** Drops everything half-typed. Called on Escape, and when the engine suspends. */
    reset: clear,

    /** The chords waiting on a longer binding. Read by the engine's hint, and by tests. */
    get pending() {
      return [...pending];
    },

    /** The repeat typed so far, as a string so "" and "0" stay distinguishable. */
    get count() {
      return count;
    },
  };
}
