"""Deciding which of Whisper's words the user actually said.

Whisper always emits text for its thirty-second window, whether or not anybody spoke
into it. Trained on subtitle-shaped data, its prior for "this window is silent" is
subtitle boilerplate -- "Thank you.", "Thanks for watching!", a music glyph. Dictation
is mostly a person thinking between sentences, so this is the median case rather than
an edge one, and a composer that fills itself with "Thank you." every time somebody
pauses is not a feature anybody keeps switched on.

The whole difficulty is that **"Thank you." is also a thing people dictate.** So none
of the rules here match on a string alone. Each one requires corroborating evidence
that the audio was not speech: how much of it survived voice-activity detection, and
how confident the model was in what it wrote. String plus evidence is an artifact;
string alone is a sentence, and deleting a user's sentence is a far worse failure than
leaving a stray "Thank you." in the box for them to delete themselves.

Every function here is pure and takes plain values, so the whole policy is testable
without a model, a microphone, or an audio file.
"""

from __future__ import annotations

import re

from app.services.voice.types import Segment, Transcript

# Boilerplate Whisper emits over silence. Matched case-insensitively, ignoring
# surrounding punctuation and whitespace, and only ever when the phrase is the entire
# transcript AND the audio was mostly not speech.
SILENCE_BOILERPLATE: frozenset[str] = frozenset(
    {
        "thank you",
        "thanks for watching",
        "thanks for watching!",
        "thank you for watching",
        "please subscribe",
        "like and subscribe",
        "subscribe to my channel",
        "bye",
        "bye bye",
        "you",
        "okay",
        "so",
        "oh",
        "the end",
    }
)

# Non-lexical markup: music glyphs, bracketed sound descriptions, and the subtitle
# credits that came with the training data. These are dropped unconditionally, at any
# position, because nobody dictates them -- there is no false-positive to trade off.
# Either a bracketed/quoted sound description, or a string that is nothing but music
# glyphs -- a lone "♪" is the commonest form and has no closing mark to match against.
_MARKUP = re.compile(r"^\s*(?:[\[(][^\])]*[\])]|[♪♫\s]+)\s*$")
_CREDITS = re.compile(
    r"(subtitles?\s+(by|provided)|transcri(bed|ption)\s+by|amara\.org|subscene|opensubtitles)",
    re.IGNORECASE,
)

# Below this, there is not enough audio for any output to be real, whatever the model
# says. A third of a second is shorter than most single words.
MIN_SPEECH_SECONDS = 0.3

# What fraction of the recording has to survive VAD before boilerplate is believed.
# Under a quarter means the clip was mostly silence, so a confident-sounding sentence
# is the model filling the void rather than reporting it.
SPEECH_RATIO_FLOOR = 0.25

# The model's own report that it was guessing. Both must hold: a genuinely quiet but
# real word can carry a high no-speech probability, and dropping it on that alone
# deletes what the user said.
NO_SPEECH_CEILING = 0.6
LOGPROB_FLOOR = -0.7

# Text that gzips unusually well is text that repeats itself, which is the signature of
# the decoder having locked into a loop. This is the same threshold Whisper uses
# internally for its temperature fallback, reused here as an output filter.
COMPRESSION_CEILING = 2.4


def _normalise(text: str) -> str:
    """Lowercase, strip surrounding punctuation and collapse spaces, for comparison only."""

    stripped = re.sub(r"[\s]+", " ", text).strip().lower()
    return stripped.strip(" .,!?…\"'“”")


def is_markup(text: str) -> bool:
    """Whether the text is a sound description or a subtitle credit rather than speech."""

    candidate = text.strip()
    if not candidate:
        return False
    return bool(_MARKUP.match(candidate) or _CREDITS.search(candidate))


def is_low_confidence(segment: Segment) -> bool:
    """Whether the model reported both "probably silence" and "and I am unsure".

    The conjunction is deliberate. Using ``or`` here would drop quiet real words, and
    silently losing speech is the failure this whole module exists to avoid causing.
    """

    return segment.no_speech_prob > NO_SPEECH_CEILING and segment.avg_logprob < LOGPROB_FLOOR


def is_repeat_loop(segment: Segment) -> bool:
    """Whether this segment is the decoder echoing itself."""

    return segment.compression_ratio > COMPRESSION_CEILING


def keep_segment(segment: Segment) -> bool:
    """Whether one segment survives on its own merits, ignoring the transcript around it."""

    if not segment.text.strip():
        return False
    if is_markup(segment.text):
        return False
    if is_repeat_loop(segment):
        return False
    return not is_low_confidence(segment)


def speech_ratio(transcript: Transcript) -> float:
    """How much of the recording VAD judged to be speech, as a fraction.

    Returns 1.0 when the durations are unknown, so a provider that does not report
    them cannot accidentally trigger the boilerplate rule.
    """

    if transcript.duration <= 0 or transcript.duration_after_vad <= 0:
        return 1.0
    return min(1.0, transcript.duration_after_vad / transcript.duration)


def is_silence_artifact(text: str, transcript: Transcript) -> bool:
    """Whether a whole transcript is boilerplate the model produced over silence.

    Requires all three: the phrase is known boilerplate, it is the *entire* output,
    and the audio was mostly not speech. "Thank you for the review, that helps" fails
    the first test; a real "Thank you." said into a live microphone fails the third.
    """

    if _normalise(text) not in SILENCE_BOILERPLATE:
        return False
    return speech_ratio(transcript) < SPEECH_RATIO_FLOOR


def clean(transcript: Transcript) -> str:
    """The transcript reduced to what the user plausibly said, or an empty string.

    Order matters: segments are filtered first so that a transcript which was *only*
    boilerplate collapses to nothing, and the whole-transcript rule then judges what
    little remains against the audio it came from.
    """

    if transcript.duration_after_vad and transcript.duration_after_vad < MIN_SPEECH_SECONDS:
        return ""

    kept = [segment.text.strip() for segment in transcript.segments if keep_segment(segment)]
    text = " ".join(part for part in kept if part).strip()
    if not text:
        return ""
    if is_silence_artifact(text, transcript):
        return ""
    return text
