/**
 * Recording a key the user is pressing on purpose.
 *
 * Two things make this less obvious than it looks. Holding Cmd fires a keydown
 * of its own before the other key arrives, so a naive recorder captures a chord
 * the moment somebody reaches for a modifier -- chordFromEvent returning null for
 * a bare modifier is what prevents that, and it is why recording goes through the
 * same normalizer as dispatch rather than reading the event itself.
 *
 * The other is knowing when a multi-chord recording has finished. "g" could be
 * the whole binding or the start of "g c", and no timeout tells them apart
 * reliably. So the caller says up front how many chords it is recording, and the
 * buffer reports itself done when it has them.
 *
 * Escape cancels rather than recording. It is not bindable, and it is the only
 * way out of a field that is swallowing every key by design.
 */

import { chordFromEvent, formatSequenceKey } from "./chord.js";

/** Enough for the longest sequence worth typing; beyond this nobody remembers it. */
export const MAX_CAPTURE_CHORDS = 3;

export function createCaptureBuffer(options = {}) {
  const maxChords = Math.max(1, Math.min(options.maxChords ?? 1, MAX_CAPTURE_CHORDS));
  let chords = [];
  let cancelled = false;

  return {
    /**
     * Offers one keydown to the recording. Returns what became of it:
     * "cancelled", "recorded" (more to come), "full" (that was the last one), or
     * "ignored" -- a bare modifier, or a key after the recording finished.
     */
    push(event) {
      if (cancelled || chords.length >= maxChords) {
        return "ignored";
      }
      if (event?.key === "Escape") {
        // Cleared as well as flagged, so a caller that only looks at `chords`
        // cannot commit half of an abandoned sequence.
        cancelled = true;
        chords = [];
        return "cancelled";
      }
      const chord = chordFromEvent(event);
      if (chord === null) {
        return "ignored";
      }
      chords.push(chord);
      return chords.length >= maxChords ? "full" : "recorded";
    },

    get chords() {
      return [...chords];
    },

    /** What would be stored, in the form the database holds. */
    get sequence() {
      return formatSequenceKey(chords);
    },

    get done() {
      return cancelled || chords.length >= maxChords;
    },

    get cancelled() {
      return cancelled;
    },

    reset() {
      chords = [];
      cancelled = false;
    },
  };
}
