"""Value types for voice input.

Dataclasses rather than loose dicts, for the same reason ``local_models/types.py``
gives: a ``Transcript`` can be built directly in a test, which is what lets the
hallucination and post-processing rules be exercised without owning a microphone or
loading a model.

``AvailabilityReport`` deliberately describes a *state the interface renders* rather
than an error anyone raises. Voice is an optional extra, and a machine that never
installed it is not broken -- it is a machine with the mic button switched off and a
sentence explaining why. This mirrors how the embedded SearXNG provider reports
itself unavailable and lets search fall back, instead of taking the process down.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# Why voice cannot be used right now. "ready" is the only value that enables the
# microphone; every other value is a sentence the settings screen shows verbatim.
Reason = Literal[
    "ready",
    "disabled",
    "dependency_missing",
    "model_not_downloaded",
    "engine_error",
]

# Where the model runs. CTranslate2 has no Metal/MPS backend, so Apple Silicon is
# "cpu" -- see faster_whisper_provider for why that is not a bug to be fixed.
Device = Literal["cpu", "cuda"]


@dataclass(frozen=True)
class AvailabilityReport:
    """Whether voice can run, and what to say when it cannot."""

    available: bool
    reason: Reason
    message: str
    remedy: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "message": self.message,
            "remedy": self.remedy,
        }


@dataclass(frozen=True)
class Segment:
    """One decoded span, with the two numbers used to decide whether to keep it.

    ``no_speech_prob`` and ``avg_logprob`` are the model's own report of how much it
    was guessing. Keeping them on the segment rather than collapsing to text at the
    provider boundary is what lets ``hallucinations.py`` stay a pure function.
    """

    text: str
    start: float
    end: float
    no_speech_prob: float = 0.0
    avg_logprob: float = 0.0
    compression_ratio: float = 1.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "no_speech_prob": round(self.no_speech_prob, 4),
            "avg_logprob": round(self.avg_logprob, 4),
            "compression_ratio": round(self.compression_ratio, 4),
        }


@dataclass(frozen=True)
class Transcript:
    """The result of one decode, before post-processing.

    ``duration`` and ``duration_after_vad`` are both kept because their *ratio* is the
    evidence the hallucination filter needs: boilerplate over mostly-silence is an
    artifact, the same words over mostly-speech are something the user said.
    """

    segments: tuple[Segment, ...] = ()
    language: str = ""
    language_probability: float = 0.0
    duration: float = 0.0
    duration_after_vad: float = 0.0

    @property
    def text(self) -> str:
        return " ".join(segment.text.strip() for segment in self.segments if segment.text.strip())


@dataclass(frozen=True)
class TranscribeOptions:
    """One decode's settings.

    Partials and finals differ only in these values, which is the whole two-tier
    design: a fast greedy pass the user watches, and one careful pass they keep.
    """

    language: str | None = "en"
    beam_size: int = 5
    vad_filter: bool = True
    initial_prompt: str | None = None
    temperature: tuple[float, ...] = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
    # Off, always. Whisper conditioning on its own previous output is what produces
    # the repeat loop, where one phrase echoes until the audio ends.
    condition_on_previous_text: bool = False
    # The decoder's own confidence guards. Named exactly as the installed
    # faster-whisper names them; see the provider for why that spelling matters.
    log_prob_threshold: float = -1.0
    no_speech_threshold: float = 0.6
    compression_ratio_threshold: float = 2.4
    vad_parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "threshold": 0.5,
            "min_silence_duration_ms": 500,
            # Generous on purpose. Too small and VAD clips word-initial fricatives and
            # plosives -- "stop" becomes "top" -- which is a worse regression than the
            # hallucinations the filter exists to prevent, and a silent one.
            "speech_pad_ms": 400,
        }
    )

    @classmethod
    def for_partial(
        cls, language: str | None, initial_prompt: str | None = None
    ) -> TranscribeOptions:
        """Fast and throwaway: the user watches this, they do not keep it.

        Greedy and single-temperature so a partial never *rewords itself* between
        passes, which reads as a bug rather than as a refinement.
        """

        return cls(
            language=language,
            beam_size=1,
            vad_filter=False,
            initial_prompt=initial_prompt,
            temperature=(0.0,),
        )

    @classmethod
    def for_final(
        cls, language: str | None, initial_prompt: str | None = None
    ) -> TranscribeOptions:
        """Careful and authoritative: this is the text that lands in the composer."""

        return cls(language=language, beam_size=5, vad_filter=True, initial_prompt=initial_prompt)
