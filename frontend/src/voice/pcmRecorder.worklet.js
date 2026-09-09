/**
 * The audio-thread half of dictation capture.
 *
 * Its whole job is to buffer and to measure. It deliberately does **no** resampling,
 * even though the audio thread is where that would be cheapest: this file is loaded
 * through `addModule()` from a Blob URL, which gives it no meaningful base URL to
 * resolve imports against, so anything it used would have to be copied in here. A
 * second copy of the anti-aliasing filter -- the one piece of this feature where a
 * subtle bug is inaudible and ruins accuracy -- is not worth the microseconds. The
 * resampling happens once, on the main thread, in the module that has the tests.
 *
 * What the audio thread *is* needed for is cadence. Timers in a background tab are
 * throttled to about one a minute; the audio thread is not throttled at all, so
 * counting frames here is the only way dictation survives the user switching tabs.
 *
 * Render quanta are 128 samples (~2.7 ms at 48 kHz). Posting each one would be some
 * 375 messages a second, so they are gathered into blocks first.
 */

// ~43 ms at 48 kHz: frequent enough that the level meter looks live, large enough that
// the message rate stays around twenty a second.
const BLOCK_SAMPLES = 2048;

class PcmRecorder extends AudioWorkletProcessor {
  constructor() {
    super();
    this._block = new Float32Array(BLOCK_SAMPLES);
    this._filled = 0;
    this._stopped = false;
    this.port.onmessage = (event) => {
      if (event.data === "stop") {
        this._flush();
        this._stopped = true;
      }
    };
  }

  _flush() {
    if (this._filled === 0) return;
    const chunk = this._block.slice(0, this._filled);

    let peak = 0;
    let sum = 0;
    for (let i = 0; i < chunk.length; i += 1) {
      const value = chunk[i];
      const magnitude = value < 0 ? -value : value;
      if (magnitude > peak) peak = magnitude;
      sum += value * value;
    }

    // Transferred, not copied: ownership moves to the main thread and nothing is
    // duplicated on a path that runs twenty times a second for minutes at a time.
    this.port.postMessage(
      { pcm: chunk, rms: Math.sqrt(sum / chunk.length), peak, sampleRate },
      [chunk.buffer],
    );
    this._filled = 0;
  }

  process(inputs) {
    if (this._stopped) return false;

    const channel = inputs[0] && inputs[0][0];
    // No channel yet is normal before the track connects; returning true keeps the
    // node alive so capture starts when it does.
    if (!channel) return true;

    for (let i = 0; i < channel.length; i += 1) {
      this._block[this._filled] = channel[i];
      this._filled += 1;
      if (this._filled === BLOCK_SAMPLES) this._flush();
    }
    return true;
  }
}

registerProcessor("pcm-recorder", PcmRecorder);
