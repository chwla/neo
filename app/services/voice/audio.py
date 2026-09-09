"""Raw PCM in, numpy out.

The browser sends 16 kHz mono little-endian signed 16-bit samples and nothing else --
no container, no codec, no header. That is a deliberate choice made on the client side
(see ``frontend/src/voice/recorder.js``): every alternative involves a compressed
container whose format differs per browser, and decoding one server-side would mean an
ffmpeg dependency for a payload we can just as easily send uncompressed at 32 KB per
second.

So there is no decoding here at all -- only a validated reinterpretation of bytes,
which is why ``numpy`` (already a dependency) is the entire toolkit.
"""

from __future__ import annotations

import numpy as np

# What the client is required to send. Whisper is trained on 16 kHz; audio at any other
# rate labelled as this one does not fail loudly, it transcribes confidently and wrongly,
# which is why the rate is fixed by the protocol rather than negotiated.
SAMPLE_RATE = 16_000
BYTES_PER_SAMPLE = 2

# Full scale for signed 16-bit. Dividing by 32768 rather than 32767 keeps the mapping
# symmetric and can never produce a value above 1.0.
_FULL_SCALE = 32_768.0


class InvalidAudio(ValueError):
    """The bytes cannot be a 16-bit PCM stream."""


def validate(payload: bytes) -> None:
    """Reject anything that cannot be signed 16-bit samples.

    An odd length is the cheap, decisive check: every sample is two bytes, so a stream
    with an odd byte count has been truncated or is not PCM at all. Catching that here
    turns a confusing transcript into a clear 400.
    """

    if len(payload) % BYTES_PER_SAMPLE:
        raise InvalidAudio("Audio must be 16-bit PCM: the byte count is odd.")


def to_float32(payload: bytes) -> np.ndarray:
    """Reinterpret PCM bytes as the float32 array the model expects, in [-1, 1)."""

    validate(payload)
    if not payload:
        return np.zeros(0, dtype=np.float32)
    samples = np.frombuffer(payload, dtype="<i2").astype(np.float32)
    return samples / _FULL_SCALE


def duration_seconds(payload: bytes) -> float:
    """How long the recording is, from its length alone."""

    return len(payload) / (BYTES_PER_SAMPLE * SAMPLE_RATE)


def rms(audio: np.ndarray) -> float:
    """Root-mean-square level, the evidence the silence rules are gated on."""

    if audio.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))


def clipped_fraction(audio: np.ndarray) -> float:
    """What share of samples sit at full scale.

    Clipping genuinely degrades recognition, unlike quiet audio, which Whisper's
    per-utterance normalisation largely absorbs. That asymmetry is why the interface
    warns about a level that is too high and says nothing about one that is too low.
    """

    if audio.size == 0:
        return 0.0
    return float(np.count_nonzero(np.abs(audio) >= 0.999) / audio.size)


def tail(payload: bytes, seconds: float) -> bytes:
    """The last ``seconds`` of a recording, aligned to a sample boundary.

    Used to bound the partial-transcription window. Whisper's encoder pads everything
    to thirty seconds regardless, so a longer window costs the same as a shorter one up
    to that point and nothing beyond it -- capping here is what stops partial passes
    growing quadratically over a long dictation.
    """

    wanted = int(seconds * SAMPLE_RATE) * BYTES_PER_SAMPLE
    if wanted <= 0 or len(payload) <= wanted:
        return payload
    return payload[-wanted:]
