/**
 * The resampler, which is the one place a silent accuracy bug can hide.
 *
 * Four properties, and the second is the one worth having. A naive decimator -- keep
 * every third sample, no filter -- passes the passband test and fails the aliasing
 * test loudly, which is exactly the regression this file exists to catch.
 */

import test from "node:test";
import assert from "node:assert/strict";

import {
  createResampler,
  floatToInt16,
  rms,
  clippedFraction,
  TARGET_RATE,
} from "../src/voice/resampler.js";

const IN_RATE = 48000;

function tone(hz, seconds, rate = IN_RATE) {
  const count = Math.floor(seconds * rate);
  const out = new Float32Array(count);
  for (let i = 0; i < count; i += 1) out[i] = Math.sin((2 * Math.PI * hz * i) / rate);
  return out;
}

/** Energy at one frequency, by correlating against a reference sine and cosine.
 *  Enough to answer "did this survive or not" without pulling in an FFT. */
function magnitudeAt(signal, hz, rate) {
  let real = 0;
  let imaginary = 0;
  for (let i = 0; i < signal.length; i += 1) {
    const angle = (2 * Math.PI * hz * i) / rate;
    real += signal[i] * Math.cos(angle);
    imaginary += signal[i] * Math.sin(angle);
  }
  return (2 * Math.hypot(real, imaginary)) / signal.length;
}

test("a speech-band tone survives the conversion", () => {
  const resampler = createResampler(IN_RATE, TARGET_RATE);
  const output = resampler.process(tone(1000, 0.5));

  assert.ok(Math.abs(output.length - 0.5 * TARGET_RATE) < 50, "output length tracks the rate ratio");
  assert.ok(magnitudeAt(output, 1000, TARGET_RATE) > 0.7, "1 kHz passes through intact");
});

test("a tone above the output Nyquist is attenuated, not folded into the speech band", () => {
  // 12 kHz cannot exist at 16 kHz. Without a low-pass it does not vanish: it aliases
  // to |16000 - 12000| = 4 kHz, landing in the middle of speech as metallic noise.
  const resampler = createResampler(IN_RATE, TARGET_RATE);
  const output = resampler.process(tone(12000, 0.5));

  const aliased = magnitudeAt(output, 4000, TARGET_RATE);
  assert.ok(aliased < 0.02, `12 kHz aliased into the speech band at ${aliased.toFixed(4)}`);
  assert.ok(rms(output) < 0.05, "and the tone is gone rather than merely moved");
});

test("splitting the input across calls gives byte-identical output", () => {
  // The filter's delay line and fractional phase have to survive between calls, or
  // there is a discontinuity at every chunk boundary -- a click every 1.5 seconds,
  // which is exactly what provokes hallucinated tokens.
  const signal = tone(700, 0.4);
  const half = Math.floor(signal.length / 2);

  const whole = createResampler(IN_RATE, TARGET_RATE).process(signal);

  const split = createResampler(IN_RATE, TARGET_RATE);
  const first = split.process(signal.subarray(0, half));
  const second = split.process(signal.subarray(half));
  const joined = new Float32Array(first.length + second.length);
  joined.set(first, 0);
  joined.set(second, first.length);

  assert.equal(joined.length, whole.length, "same number of output samples");
  for (let i = 0; i < whole.length; i += 1) {
    assert.ok(
      Math.abs(whole[i] - joined[i]) < 1e-6,
      `sample ${i} differs: ${whole[i]} vs ${joined[i]}`,
    );
  }
});

test("a matching input rate is passed through untouched", () => {
  const input = tone(440, 0.05, TARGET_RATE);
  const output = createResampler(TARGET_RATE, TARGET_RATE).process(input);
  assert.deepEqual(Array.from(output), Array.from(input));
});

test("out-of-range samples saturate rather than wrapping", () => {
  // A wrap turns an over-loud sample into a full-scale sample of the opposite sign:
  // an impulse the speaker never produced.
  const output = floatToInt16(Float32Array.from([1.5, -1.5, 0, 1, -1]));
  assert.deepEqual(Array.from(output), [32767, -32768, 0, 32767, -32768]);
});

test("level helpers report what the meter and the too-loud warning need", () => {
  assert.equal(rms(new Float32Array(0)), 0);
  assert.ok(Math.abs(rms(Float32Array.from([1, -1, 1, -1])) - 1) < 1e-9);
  assert.equal(clippedFraction(Float32Array.from([1, 0, 0, 0])), 0.25);
  assert.equal(clippedFraction(Float32Array.from([0.5, 0.5])), 0);
});

test("every rate a real microphone reports converts to the wire format", () => {
  // 44100 and 48000 are what hardware actually gives; 16000 is what Chrome hands back
  // when it honours the AudioContext hint, and must pass through untouched.
  for (const rate of [16000, 44100, 48000]) {
    const seconds = 0.25;
    const resampler = createResampler(rate, TARGET_RATE);
    const output = resampler.process(tone(440, seconds, rate));
    const expected = seconds * TARGET_RATE;

    assert.ok(
      Math.abs(output.length - expected) < 40,
      `${rate} Hz gave ${output.length} samples, expected about ${expected}`,
    );
    assert.ok(magnitudeAt(output, 440, TARGET_RATE) > 0.6, `${rate} Hz lost the tone`);

    // The backend reinterprets these bytes as little-endian signed 16-bit and
    // divides by 32768. Anything else here is silently wrong rather than an error.
    const pcm = floatToInt16(output);
    assert.equal(pcm.BYTES_PER_ELEMENT, 2, "16-bit");
    assert.equal(pcm.byteLength, output.length * 2);
    assert.ok(pcm.every((v) => v >= -32768 && v <= 32767), "in range");
  }
});

test("a very short recording still produces samples", () => {
  // 30 ms is about what a tapped hotkey yields, and an off-by-one in the filter's
  // priming would return an empty buffer for it.
  const output = createResampler(48000, TARGET_RATE).process(tone(440, 0.03, 48000));
  assert.ok(output.length > 100, `expected samples, got ${output.length}`);
});

test("a long recording stays on the output grid", () => {
  // Drift accumulates: a fractional phase that is not carried between calls loses a
  // sample every few seconds, and a five-minute dictation ends up audibly out of step.
  const resampler = createResampler(44100, TARGET_RATE);
  let total = 0;
  const seconds = 30;
  for (let i = 0; i < seconds; i += 1) total += resampler.process(tone(300, 1, 44100)).length;
  assert.ok(
    Math.abs(total - seconds * TARGET_RATE) < TARGET_RATE * 0.001,
    `${seconds}s drifted to ${total} samples (expected ~${seconds * TARGET_RATE})`,
  );
});
