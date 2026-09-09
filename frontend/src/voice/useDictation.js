/**
 * Dictation, wired to the composer.
 *
 * The state lives in `dictationMachine.js`; this owns the side effects — the
 * microphone, the request, and the teardown. Two invariants are worth naming because
 * everything awkward here exists to hold them:
 *
 * *Nothing is ever sent.* The transcript is handed to the caller, who puts it in the
 * composer. The user reads it and presses Enter themselves.
 *
 * *A cancelled dictation leaves no trace.* Recording is asynchronous and so is
 * transcription, so a result can arrive after the user has given up on it. Every
 * asynchronous step therefore carries the session number it started under and drops
 * its result if that number has moved on. Without it, pressing Escape and then typing
 * gets you a transcript pasted into the middle of your sentence a second later.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../api.js";
import {
  ERROR,
  IDLE,
  PROCESSING,
  RECORDING,
  STARTING,
  can,
  isBusy,
} from "./dictationMachine.js";
import { captureSupport, startRecording } from "./recorder.js";

/** Beyond this the recording stops itself and transcribes what it has. Five minutes is
 *  far longer than a chat message needs, and it bounds the buffers on both sides. */
const MAX_SECONDS = 300;
const WARN_SECONDS = 270;

export function useDictation({ onText, onError } = {}) {
  const [state, setState] = useState(IDLE);
  const [seconds, setSeconds] = useState(0);
  const [level, setLevel] = useState(0);
  const [error, setError] = useState(null);

  const sessionRef = useRef(null);
  const tickRef = useRef(null);
  const abortRef = useRef(null);
  /* Bumped on every start and every cancel. An async step that comes back holding an
     old number is from a dictation nobody is waiting for any more. */
  const runRef = useRef(0);
  const stateRef = useRef(IDLE);

  const setPhase = useCallback((phase) => {
    stateRef.current = phase;
    setState(phase);
  }, []);

  const stopClock = useCallback(() => {
    if (tickRef.current) {
      clearInterval(tickRef.current);
      tickRef.current = null;
    }
  }, []);

  /** Close the microphone and free the audio graph. Safe to call twice. */
  const releaseMicrophone = useCallback(async () => {
    stopClock();
    const session = sessionRef.current;
    sessionRef.current = null;
    if (!session) return null;
    try {
      return await session.stop();
    } catch {
      // A device that vanished throws on teardown; there is nothing to recover and
      // nothing the user can do, and reporting it would replace their transcript with
      // an error about a microphone they have already unplugged.
      return null;
    }
  }, [stopClock]);

  const fail = useCallback(
    (detail) => {
      setError(detail);
      setPhase(ERROR);
      setSeconds(0);
      setLevel(0);
    },
    [setPhase],
  );

  const cancel = useCallback(async () => {
    if (!can(stateRef.current, "cancel")) return;
    runRef.current += 1; // everything still in flight is now stale
    abortRef.current?.abort();
    abortRef.current = null;
    setPhase(IDLE);
    setSeconds(0);
    setLevel(0);
    setError(null);
    await releaseMicrophone();
  }, [releaseMicrophone, setPhase]);

  const transcribe = useCallback(
    async (pcm, run) => {
      if (!pcm || pcm.length === 0) {
        if (runRef.current === run) setPhase(IDLE);
        return;
      }
      const controller = new AbortController();
      abortRef.current = controller;

      let text = "";
      let empty = false;
      let failure = null;
      try {
        await api.transcribeVoice(pcm, {
          signal: controller.signal,
          onEvent: (event) => {
            if (event.type === "final") {
              text = event.text ?? "";
              empty = Boolean(event.empty);
            } else if (event.type === "error") {
              failure = event.detail || "That could not be transcribed.";
            }
          },
        });
      } catch (caught) {
        failure = caught?.name === "AbortError" ? null : caught.message || String(caught);
      } finally {
        if (abortRef.current === controller) abortRef.current = null;
      }

      // The whole point of the run token: a cancelled dictation must not insert.
      if (runRef.current !== run) return;

      if (failure) {
        fail({ code: "transcribe_failed", message: failure });
        onError?.(new Error(failure));
        return;
      }
      if (text) onText?.(text);
      else if (empty) setError({ code: "no_speech", message: "I didn't catch anything." });
      setPhase(IDLE);
    },
    [fail, onError, onText, setPhase],
  );

  const stop = useCallback(async () => {
    if (!can(stateRef.current, "stop")) return;
    const run = runRef.current;
    setPhase(PROCESSING);
    setLevel(0);
    const pcm = await releaseMicrophone();
    if (runRef.current !== run) return; // cancelled while the stream was closing
    setSeconds(0);
    await transcribe(pcm, run);
  }, [releaseMicrophone, setPhase, transcribe]);

  const start = useCallback(async () => {
    if (!can(stateRef.current, "start")) return;
    setError(null);

    const support = captureSupport();
    if (!support.supported) {
      fail({ code: support.reason, message: support.message, remedy: support.remedy });
      return;
    }

    runRef.current += 1;
    const run = runRef.current;
    setPhase(STARTING);

    try {
      const session = await startRecording({
        onLevel: ({ rms }) => {
          if (runRef.current === run) setLevel(rms);
        },
        onError: (caught) => {
          if (runRef.current !== run) return;
          // A track that ended is a device unplugged or a permission revoked. Finish
          // rather than discard: the words already spoken are still worth having.
          if (caught?.code === "track_ended") stop();
          else onError?.(caught);
        },
      });

      // Cancelled while the permission prompt was up: close what we just opened
      // rather than starting a recording nobody asked for any more.
      if (runRef.current !== run) {
        await session.stop().catch(() => {});
        return;
      }

      sessionRef.current = session;
      setPhase(RECORDING);

      tickRef.current = setInterval(() => {
        if (runRef.current !== run) return;
        const elapsed = session.seconds;
        setSeconds(elapsed);
        if (elapsed >= MAX_SECONDS) stop();
      }, 250);
    } catch (caught) {
      if (runRef.current !== run) return;
      fail({
        code: caught?.code ?? "capture_failed",
        message: caught?.message ?? "The microphone could not be started.",
        remedy: caught?.remedy ?? "",
      });
    }
  }, [fail, onError, setPhase, stop]);

  const toggle = useCallback(() => {
    if (stateRef.current === RECORDING) stop();
    else if (can(stateRef.current, "start")) start();
  }, [start, stop]);

  /* Unmounting mid-recording must still release the device, or the browser's recording
     indicator stays lit with nothing behind it. */
  useEffect(
    () => () => {
      runRef.current += 1;
      stopClock();
      abortRef.current?.abort();
      sessionRef.current?.stop?.().catch(() => {});
      sessionRef.current = null;
    },
    [stopClock],
  );

  return {
    state,
    seconds,
    level,
    error,
    recording: state === RECORDING,
    busy: isBusy(state),
    nearLimit: seconds >= WARN_SECONDS,
    maxSeconds: MAX_SECONDS,
    start,
    stop,
    cancel,
    toggle,
    dismissError: () => {
      setError(null);
      setPhase(IDLE);
    },
  };
}
