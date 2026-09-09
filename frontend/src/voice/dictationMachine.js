/**
 * The dictation state machine, as data.
 *
 * Pulled out of the React hook so the rules can be tested without a microphone, an
 * AudioContext or a DOM -- the frontend suite has none of those. The hook owns the
 * side effects; this owns what is legal.
 *
 *     idle ──start──▶ starting ──ready──▶ recording ──stop──▶ processing ──▶ idle
 *       ▲                 │                    │                  │
 *       └────cancel───────┴────────────────────┴──────────────────┘
 *                         └──fail──▶ error ──dismiss/start──▶ …
 *
 * Every transition is explicit. The previous version coordinated three booleans, and
 * the states that mattered -- "stopping, but a transcription request is already in
 * flight" -- were the ones that could not be named.
 */

export const IDLE = "idle";
export const STARTING = "starting";
export const RECORDING = "recording";
export const PROCESSING = "processing";
export const ERROR = "error";

const TRANSITIONS = {
  [IDLE]: { start: STARTING },
  [STARTING]: { ready: RECORDING, fail: ERROR, cancel: IDLE },
  [RECORDING]: { stop: PROCESSING, cancel: IDLE, fail: ERROR },
  // Cancel is legal here too, and it matters: it is what stops a transcript that is
  // already being decoded from landing in the composer after the user gave up on it.
  [PROCESSING]: { done: IDLE, cancel: IDLE, fail: ERROR },
  [ERROR]: { start: STARTING, dismiss: IDLE, cancel: IDLE },
};

/** The state an event leads to, or null when the event does not apply. */
export function next(state, event) {
  return TRANSITIONS[state]?.[event] ?? null;
}

export function can(state, event) {
  return next(state, event) !== null;
}

/** Whether the microphone is open, and so whether there is anything to tear down. */
export function isCapturing(state) {
  return state === STARTING || state === RECORDING;
}

/** Whether the user should be shown that something is happening. */
export function isBusy(state) {
  return state === STARTING || state === PROCESSING;
}
