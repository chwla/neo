"""The speech-recognition engine interface.

One Protocol with four methods, so that the engine is a replaceable part. Today there
is exactly one implementation, running Whisper locally; a hosted service would satisfy
the same four methods and nothing above this line would change.

``available()`` returns a report rather than raising. A machine that never installed
the optional extra is not a machine in an error state -- it is one where the microphone
button is switched off with a sentence saying why. This is the same shape the embedded
SearXNG provider uses when its optional setup was skipped, and it is what keeps a
missing dependency from turning into a stack trace on a screen.
"""

from __future__ import annotations

import threading
from typing import Protocol

import numpy as np

from app.core.config import get_settings
from app.services.voice.types import AvailabilityReport, TranscribeOptions, Transcript


class TranscriptionProvider(Protocol):
    """What voice input needs from an engine, and nothing more."""

    name: str

    def available(self) -> AvailabilityReport:
        """Whether a transcription would work right now, and what to say if not."""

    def warm(self) -> None:
        """Load and specialise the model. Idempotent, blocking, safe to call twice."""

    def transcribe(
        self,
        audio: np.ndarray,
        options: TranscribeOptions,
        cancel: threading.Event | None = None,
    ) -> Transcript:
        """Decode one utterance. ``cancel`` is checked between segments, not within one."""

    def unload(self) -> None:
        """Release the model's memory. A later call reloads it."""


def build() -> TranscriptionProvider:
    """The configured engine.

    Selected by name so that adding one is a branch here rather than an edit spread
    across the service and the routes.
    """

    from app.services.voice.faster_whisper_provider import FasterWhisperProvider

    configured = (get_settings().voice_provider or "faster_whisper").strip().lower()
    if configured in ("faster_whisper", "faster-whisper", "local", ""):
        return FasterWhisperProvider()
    raise ValueError(f"No speech engine called '{configured}'.")
