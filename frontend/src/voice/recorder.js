/**
 * Microphone capture, from permission prompt to 16 kHz PCM.
 *
 * Deliberately not `MediaRecorder`: it yields WebM/Opus in Chrome and MP4/AAC in
 * Safari, and decoding either on the server would mean an ffmpeg dependency for a
 * payload we can just as well send uncompressed at 32 KB a second.
 *
 * The worklet module is loaded from a Blob built out of `?raw` source. Neither of the
 * obvious alternatives works here: `frontend/public/` is served by the SPA catch-all
 * as `index.html`, and `addModule()` rejects a non-JavaScript MIME type; and `?url`
 * lets Vite inline small scripts as `data:` URLs and can pull its module-preload
 * helper into the worklet scope, where it throws. `?raw` behaves identically in dev,
 * in `vite build`, and under the single-origin FastAPI deployment.
 */

import workletSource from "./pcmRecorder.worklet.js?raw";
import { createResampler, floatToInt16, TARGET_RATE, rms } from "./resampler.js";

export const SAMPLE_RATE = TARGET_RATE;

/** How much audio accumulates before a partial chunk is sent.
 *
 *  Whisper's encoder pads everything to a thirty-second window, so decoding one second
 *  costs almost exactly what decoding eight does -- sub-second chunks buy latency that
 *  is already lost and spend CPU that is not free. */
export const CHUNK_SECONDS = 1.5;

/** Why capture could not start, in words the interface can show verbatim. */
export function describeCaptureError(error) {
  const name = error?.name ?? "";
  if (name === "NotAllowedError" || name === "SecurityError") {
    return {
      code: "permission_denied",
      message: "Neo needs permission to use your microphone.",
      remedy: "Allow the microphone in your browser's site settings, then try again.",
    };
  }
  if (name === "NotFoundError" || name === "DevicesNotFoundError") {
    return { code: "no_device", message: "No microphone was found.", remedy: "" };
  }
  if (name === "NotReadableError" || name === "TrackStartError") {
    return {
      code: "device_busy",
      message: "Your microphone is being used by another application.",
      remedy: "",
    };
  }
  return { code: "capture_failed", message: "The microphone could not be started.", remedy: "" };
}

/**
 * Whether dictation can run in this page at all.
 *
 * `getUserMedia` and `AudioWorklet` both require a secure context, which means HTTPS
 * or localhost. Reaching Neo at a LAN address over plain HTTP -- exactly how somebody
 * would open a container from another machine -- silently has no microphone, and
 * saying so plainly is much better than a button that does nothing.
 */
export function captureSupport() {
  if (typeof window === "undefined") return { supported: false, reason: "unsupported_context" };
  if (!window.isSecureContext) {
    return {
      supported: false,
      reason: "insecure_context",
      message: "Voice input needs a secure connection.",
      remedy: "Open Neo at localhost, or put HTTPS in front of it.",
    };
  }
  if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) {
    return {
      supported: false,
      reason: "unsupported_context",
      message: "This browser cannot record audio.",
      remedy: "",
    };
  }
  return { supported: true, reason: "ready" };
}

/**
 * Start capturing.
 *
 * `onChunk` receives roughly `CHUNK_SECONDS` of Int16 PCM at a time; `onLevel` fires
 * about twenty times a second for the meter. `stop()` resolves with the whole
 * recording, which the caller keeps -- so a chunk that failed to upload costs nothing
 * but a partial, and the authoritative transcription still sees every word.
 */
export async function startRecording({ onChunk, onLevel, onError } = {}) {
  const support = captureSupport();
  if (!support.supported) throw Object.assign(new Error(support.message), support);

  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        // Not cosmetic. Automatic gain control alone decides whether somebody sitting
        // back from a laptop microphone is transcribable at all, and echo cancellation
        // is what stops the speakers' own output being transcribed back.
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
  } catch (error) {
    if (error?.name === "OverconstrainedError") {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } else {
      throw Object.assign(new Error(describeCaptureError(error).message), describeCaptureError(error));
    }
  }

  // Ask for 16 kHz directly: where the browser honours it, its own resampler is better
  // than ours and runs in native code. Where it does not, `resampler.js` takes over --
  // which is why both paths have to exist and why the rate is read back rather than
  // assumed.
  let context;
  try {
    context = new AudioContext({ sampleRate: SAMPLE_RATE, latencyHint: "interactive" });
  } catch {
    context = new AudioContext();
  }
  if (context.state === "suspended") await context.resume();

  const moduleUrl = URL.createObjectURL(new Blob([workletSource], { type: "text/javascript" }));
  try {
    await context.audioWorklet.addModule(moduleUrl);
  } finally {
    URL.revokeObjectURL(moduleUrl);
  }

  const source = context.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(context, "pcm-recorder");

  const resampler = createResampler(context.sampleRate, SAMPLE_RATE);
  const everything = [];
  let pending = [];
  let pendingLength = 0;
  let stopped = false;

  const chunkTarget = Math.round(CHUNK_SECONDS * SAMPLE_RATE);

  function drain(force) {
    while (pendingLength >= chunkTarget || (force && pendingLength > 0)) {
      const take = Math.min(pendingLength, chunkTarget);
      const chunk = new Int16Array(take);
      let offset = 0;
      while (offset < take) {
        const head = pending[0];
        const wanted = Math.min(head.length, take - offset);
        chunk.set(head.subarray(0, wanted), offset);
        offset += wanted;
        if (wanted === head.length) pending.shift();
        else pending[0] = head.subarray(wanted);
      }
      pendingLength -= take;
      onChunk?.(chunk);
      if (force && pendingLength === 0) return;
    }
  }

  node.port.onmessage = (event) => {
    if (stopped) return;
    try {
      const { pcm, rms: level, peak } = event.data;
      const resampled = resampler.process(pcm);
      const samples = floatToInt16(resampled);
      everything.push(samples);
      pending.push(samples);
      pendingLength += samples.length;
      onLevel?.({ rms: level ?? rms(resampled), peak: peak ?? 0 });
      drain(false);
    } catch (error) {
      onError?.(error);
    }
  };

  source.connect(node);
  // Not connected to the destination: routing the microphone to the speakers would
  // play the user's own voice back at them.

  async function teardown() {
    stopped = true;
    try {
      node.port.postMessage("stop");
    } catch {
      /* the node may already be gone */
    }
    try {
      source.disconnect();
      node.disconnect();
    } catch {
      /* likewise */
    }
    // Both matter: a live track leaves the browser's recording indicator lit, which
    // people rightly read as "it is still listening".
    stream.getTracks().forEach((track) => track.stop());
    try {
      await context.close();
    } catch {
      /* already closed */
    }
  }

  // A track can end on its own -- the device is unplugged, or permission is revoked
  // mid-sentence. That must finalise what was already said rather than discard it.
  stream.getTracks().forEach((track) => {
    track.addEventListener("ended", () => onError?.(Object.assign(new Error("ended"), { code: "track_ended" })));
  });

  return {
    sampleRate: SAMPLE_RATE,
    inputRate: context.sampleRate,
    get seconds() {
      return everything.reduce((total, part) => total + part.length, 0) / SAMPLE_RATE;
    },
    /** Everything captured so far, as one buffer. The client keeps this so a failed
     *  chunk upload never costs a word. */
    recording() {
      const total = everything.reduce((sum, part) => sum + part.length, 0);
      const merged = new Int16Array(total);
      let offset = 0;
      for (const part of everything) {
        merged.set(part, offset);
        offset += part.length;
      }
      return merged;
    },
    async stop() {
      drain(true);
      await teardown();
      return this.recording();
    },
  };
}
