/**
 * Getting microphone audio to 16 kHz without wrecking it.
 *
 * Whisper is trained on 16 kHz. Audio at any other rate handed over as though it were
 * 16 kHz does not fail loudly -- it transcribes confidently and wrongly, which is the
 * worst kind of bug to own. Microphones hand us 48000 or 44100, so something has to
 * convert, and the conversion has to be done properly.
 *
 * "Properly" means one thing above all: **low-pass before decimating.** Dropping every
 * third sample of a 48 kHz stream is the obvious implementation and it aliases --
 * energy above 8 kHz folds back down *into the speech band* as inharmonic metallic
 * noise. Whisper's mel front end reads that as structure, so it does not sound like a
 * bug, it reads like the model being bad at its job.
 *
 * The second requirement is less obvious and just as important: the filter is
 * **stateful across calls**. Audio arrives in 128-sample blocks and leaves in chunks
 * every second and a half. A filter that reset per block would put a discontinuity at
 * every boundary, and a periodic click is precisely the kind of artifact that provokes
 * hallucinated tokens.
 */

export const TARGET_RATE = 16000;

/** Where the anti-alias filter rolls off: 0.45 of the output rate, comfortably below
 *  the 8 kHz Nyquist limit while leaving the whole speech band intact. */
const CUTOFF_RATIO = 0.45;

/** Filter length. Longer is sharper and costs more; 96 taps at 48 kHz is a few
 *  thousand multiply-accumulates per 128-sample render quantum, which is nothing
 *  against a 2.67 ms deadline. */
const DEFAULT_TAPS = 96;

/**
 * A windowed-sinc low-pass, Blackman-windowed.
 *
 * Blackman rather than a rectangular window because an abrupt truncation of the sinc
 * produces ripple that lets aliased energy through. Linear phase, so plosives are not
 * smeared in time.
 */
export function designLowPass(cutoffHz, sampleRate, taps = DEFAULT_TAPS) {
  const length = taps % 2 === 0 ? taps + 1 : taps; // odd, so there is a true centre tap
  const coefficients = new Float32Array(length);
  const centre = (length - 1) / 2;
  const omega = (2 * Math.PI * cutoffHz) / sampleRate;
  let sum = 0;

  for (let i = 0; i < length; i += 1) {
    const n = i - centre;
    const sinc = n === 0 ? omega : Math.sin(omega * n) / n;
    const blackman =
      0.42 -
      0.5 * Math.cos((2 * Math.PI * i) / (length - 1)) +
      0.08 * Math.cos((4 * Math.PI * i) / (length - 1));
    const value = sinc * blackman;
    coefficients[i] = value;
    sum += value;
  }

  // Normalise to unity gain at DC, so the filter changes the spectrum and not the level.
  for (let i = 0; i < length; i += 1) coefficients[i] /= sum;
  return coefficients;
}

/**
 * A resampler that remembers where it was.
 *
 * `process` may be called with any number of samples, any number of times, and the
 * output is identical to what one call with the concatenated input would produce.
 * That property is the whole point, and it is what the chunk-boundary test pins down.
 */
export function createResampler(inputRate, outputRate = TARGET_RATE, taps = DEFAULT_TAPS) {
  if (!Number.isFinite(inputRate) || inputRate <= 0) {
    throw new Error(`Invalid input rate: ${inputRate}`);
  }

  // Already there: nothing to filter, nothing to interpolate, no state to keep.
  if (inputRate === outputRate) {
    return { process: (input) => Float32Array.from(input), reset() {} };
  }

  const coefficients = designLowPass(CUTOFF_RATIO * outputRate, inputRate, taps);
  const length = coefficients.length;
  // The delay line holds the tail of the previous call, which is exactly what makes
  // the filter continuous across block boundaries.
  let history = new Float32Array(length - 1);
  const step = inputRate / outputRate;
  let phase = 0;

  function filtered(buffer, at) {
    // `at` indexes the filter's centre tap; the sum walks the whole window around it.
    let total = 0;
    for (let k = 0; k < length; k += 1) {
      const index = at - k;
      if (index >= 0 && index < buffer.length) total += buffer[index] * coefficients[k];
    }
    return total;
  }

  return {
    process(input) {
      if (!input || input.length === 0) return new Float32Array(0);

      // Prepend the retained tail so the first output samples see real history rather
      // than zeros, which would be an audible click at every boundary.
      const buffer = new Float32Array(history.length + input.length);
      buffer.set(history, 0);
      buffer.set(input, history.length);

      const output = [];
      // Positions are measured in the concatenated buffer; `phase` carries the
      // fractional remainder between calls so the output grid never drifts.
      let position = history.length + phase;
      while (position < buffer.length) {
        const base = Math.floor(position);
        const fraction = position - base;
        // Linear interpolation between two filtered neighbours. The filter has already
        // removed everything above the output Nyquist, so what remains is smooth at
        // this scale and a higher-order interpolator buys nothing measurable.
        const a = filtered(buffer, base);
        const b = filtered(buffer, base + 1);
        output.push(a + (b - a) * fraction);
        position += step;
      }

      phase = position - buffer.length;
      const keep = Math.min(length - 1, buffer.length);
      history = buffer.slice(buffer.length - keep);
      return Float32Array.from(output);
    },
    reset() {
      history = new Float32Array(length - 1);
      phase = 0;
    },
  };
}

/**
 * Float samples to signed 16-bit, the format the wire and the model both want.
 *
 * Clamped rather than wrapped: a sample above full scale that wraps becomes a
 * full-amplitude sample of the opposite sign, which is an impulse -- audibly a click,
 * and to a recogniser a transient that was never spoken.
 */
export function floatToInt16(input) {
  const output = new Int16Array(input.length);
  for (let i = 0; i < input.length; i += 1) {
    const value = Math.max(-1, Math.min(1, input[i]));
    output[i] = value < 0 ? Math.round(value * 32768) : Math.round(value * 32767);
  }
  return output;
}

/** Root-mean-square level, for the input meter and the too-loud warning. */
export function rms(input) {
  if (!input || input.length === 0) return 0;
  let total = 0;
  for (let i = 0; i < input.length; i += 1) total += input[i] * input[i];
  return Math.sqrt(total / input.length);
}

/** What share of samples sit at full scale. Clipping genuinely degrades recognition,
 *  which is why the interface warns about a level that is too high and stays quiet
 *  about one that is too low. */
export function clippedFraction(input) {
  if (!input || input.length === 0) return 0;
  let count = 0;
  for (let i = 0; i < input.length; i += 1) if (Math.abs(input[i]) >= 0.999) count += 1;
  return count / input.length;
}
