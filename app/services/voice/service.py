"""Orchestration for voice input.

Routes stay thin: they parse and validate, this decides. Everything returned from here
is already shaped for the interface, including the sentence to show when speech
recognition cannot run.

The order of operations in ``transcribe`` is the accuracy policy, and it is worth
reading as one piece: refuse silence before spending anything on it, decode, retry once
if voice-activity detection swallowed a quiet speaker whole, drop what the model itself
reports as guesswork, remove a prompt it echoed instead of transcribing, and only then
tidy the text.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.services.voice import audio as audio_io
from app.services.voice import hallucinations, models, postprocess
from app.services.voice.provider import TranscriptionProvider, build
from app.services.voice.types import AvailabilityReport, TranscribeOptions, Transcript
from app.services.voice.worker import Busy, worker

# Below this level the recording is a microphone left open, not speech. Checking it
# costs one pass over an array and removes an entire class of hallucination before the
# model is ever asked -- Whisper given silence does not return nothing, it invents.
SILENCE_RMS_FLOOR = 0.002

# How long a partial pass may look back. Whisper pads everything to thirty seconds
# anyway, so a longer window costs the same up to that point and nothing is gained past
# it; the cap is what stops partial work growing quadratically over a long dictation.
PARTIAL_WINDOW_SECONDS = 30.0

_provider: TranscriptionProvider | None = None
_provider_lock = threading.Lock()


def provider() -> TranscriptionProvider:
    """The process-wide engine. One model, loaded once, shared under its own lock."""

    global _provider
    with _provider_lock:
        if _provider is None:
            _provider = build()
        return _provider


def set_provider(replacement: TranscriptionProvider | None) -> None:
    """Swap the engine. Tests use this to run the whole stack without a model."""

    global _provider
    with _provider_lock:
        _provider = replacement


class VoiceService:
    """What the routes call."""

    def __init__(self, engine: TranscriptionProvider | None = None) -> None:
        self._engine = engine or provider()

    # -- status ------------------------------------------------------------------

    def availability(self) -> AvailabilityReport:
        try:
            return self._engine.available()
        except Exception as exc:  # a broken engine is a state, not a crash
            return AvailabilityReport(
                available=False,
                reason="engine_error",
                message="Speech recognition could not start.",
                remedy=str(exc),
            )

    def status(self) -> dict[str, Any]:
        settings = get_settings()
        report = self.availability()
        model_id = settings.voice_model
        entry = models.get(model_id)

        # A download in flight outranks "not downloaded": the interface should say
        # what is happening rather than offer a button that starts a second one.
        downloading = models.active_state(model_id)
        reason = report.reason
        message = report.message
        remedy = report.remedy
        if downloading and reason == "model_not_downloaded":
            reason = "model_downloading"
            message = "The transcription model is downloading."
            remedy = ""

        return {
            "enabled": bool(settings.voice_enabled),
            "provider": getattr(self._engine, "name", "unknown"),
            "available": report.available,
            "reason": reason,
            "message": message,
            "remedy": remedy,
            "model": {
                **(entry.as_dict() if entry else {"id": model_id, "label": model_id}),
                "installed": self.model_root_has(model_id),
                "downloading": bool(downloading),
                "percent": (downloading or {}).get("percent", 0),
            },
            "language": settings.voice_language,
            "sample_rate": audio_io.SAMPLE_RATE,
            "max_seconds": settings.voice_max_seconds,
            "chunk_seconds": 1.5,
        }

    # -- models ------------------------------------------------------------------

    def model_root(self) -> Path:
        from app.services.voice.faster_whisper_provider import model_root

        return model_root()

    def model_root_has(self, model_id: str) -> bool:
        try:
            return models.is_installed(self.model_root(), model_id)
        except Exception:
            return False

    def models(self) -> dict[str, Any]:
        """The catalogue, with what is on disk. Never raises: the settings screen has
        to render even when the model directory is unreadable."""

        root = self.model_root()
        selected = get_settings().voice_model
        listed = []
        for entry in models.CATALOG:
            active = models.active_state(entry.id)
            listed.append(
                {
                    **entry.as_dict(),
                    "installed": self.model_root_has(entry.id),
                    "installed_bytes": models.installed_bytes(root, entry.id),
                    "downloading": bool(active),
                    "percent": (active or {}).get("percent", 0),
                    "selected": entry.id == selected,
                }
            )
        return {"models": listed, "selected": selected}

    def install_model(self, model_id: str) -> Iterator[dict[str, Any]]:
        """Download a model, yielding progress. Only catalogue ids are accepted."""

        yield from models.download(self.model_root(), model_id)

    # -- transcription -----------------------------------------------------------

    def _decode(
        self, payload: bytes, options: TranscribeOptions, cancel: threading.Event | None
    ) -> tuple[Transcript, bool]:
        """One decode plus the quiet-speaker retry, and whether that retry happened."""

        samples = audio_io.to_float32(payload)
        transcript = self._engine.transcribe(samples, options, cancel)

        # Voice-activity detection has its own failure mode: a quiet speaker or a
        # low-gain microphone can produce zero detected speech, and the user is told
        # nothing was heard when in fact they spoke. One retry without it, relying on
        # the confidence thresholds instead, is what stops that reading as "broken".
        if (
            options.vad_filter
            and transcript.duration > 1.0
            and transcript.duration_after_vad <= 0.0
        ):
            relaxed = TranscribeOptions(
                language=options.language,
                beam_size=options.beam_size,
                vad_filter=False,
                initial_prompt=options.initial_prompt,
                temperature=options.temperature,
            )
            return self._engine.transcribe(samples, relaxed, cancel), True
        return transcript, False

    def transcribe(
        self,
        payload: bytes,
        *,
        language: str | None = None,
        initial_prompt: str | None = None,
        partial: bool = False,
        spoken_punctuation: bool = False,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Turn a recording into text fit for the composer."""

        audio_io.validate(payload)
        if partial:
            payload = audio_io.tail(payload, PARTIAL_WINDOW_SECONDS)

        samples = audio_io.to_float32(payload)
        duration = audio_io.duration_seconds(payload)
        if samples.size == 0 or audio_io.rms(samples) < SILENCE_RMS_FLOOR:
            return {"text": "", "language": language or "", "duration": duration, "empty": True}

        settings = get_settings()
        pinned = language if language is not None else settings.voice_language
        # An empty pin means detect. Partials never detect: one and a half seconds is
        # far too little to identify a language from, and a provisional transcript that
        # changes language mid-sentence is worse than one that is slightly wrong.
        if partial and not pinned:
            pinned = "en"

        options = (
            TranscribeOptions.for_partial(pinned or None, initial_prompt)
            if partial
            else TranscribeOptions.for_final(pinned or None, initial_prompt)
        )

        transcript, retried = self._decode(payload, options, cancel)
        text = hallucinations.clean(transcript)
        text = postprocess.strip_prompt_echo(text, initial_prompt)
        text = postprocess.finalise(text, spoken_punctuation=spoken_punctuation)

        return {
            "text": text,
            "language": transcript.language or (pinned or ""),
            "language_probability": round(transcript.language_probability, 4),
            "duration": round(transcript.duration or duration, 3),
            "duration_after_vad": round(transcript.duration_after_vad, 3),
            "clipped": round(audio_io.clipped_fraction(samples), 4),
            "retried_without_vad": retried,
            "empty": not text,
        }

    def transcribe_queued(self, payload: bytes, **kwargs: Any) -> dict[str, Any]:
        """Transcribe on the shared worker, refusing when it is already saturated."""

        future = worker().submit(lambda: self.transcribe(payload, **kwargs))
        return future.result()

    def warm(self) -> None:
        self._engine.warm()


def service() -> VoiceService:
    return VoiceService()


__all__ = ["Busy", "VoiceService", "service", "set_provider", "provider"]
