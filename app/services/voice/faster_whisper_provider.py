"""Whisper, running on this machine, through faster-whisper.

Everything in here is a decision about accuracy, and each one is written down with the
failure it prevents, because none of them are obvious and most of them are invisible
when wrong -- a mis-set flag does not crash, it quietly returns confident nonsense.

The import is deliberately inside a function. ``faster-whisper`` is an optional extra
weighing a few hundred megabytes, and ``app.main`` has to start on a machine that has
never installed it.

**On Apple Silicon this runs on the CPU, and that is not a bug.** CTranslate2, the
runtime underneath faster-whisper, has no Metal or MPS backend. There is no GPU flag to
find. The int8 path over ARM NEON is the fast route on those machines, and it is what
the defaults below select.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import numpy as np

from app.core.config import get_base_settings, get_settings
from app.services.voice.audio import SAMPLE_RATE
from app.services.voice.types import (
    AvailabilityReport,
    Segment,
    TranscribeOptions,
    Transcript,
)


# Model files are machine-level, not profile-level. ``get_settings().data_dir`` is
# rewritten per profile inside a request, so building the path from it would download
# the same gigabyte once for every profile on the machine.
def model_root() -> Path:
    configured = (get_settings().voice_model_dir or "").strip()
    if configured:
        return Path(configured)
    # ``data_dir`` is unset outside a container, where the other storage settings
    # fall back to relative ``data/`` paths; match that rather than inventing one.
    base = get_base_settings().data_dir
    return (Path(base) if base else Path("data")) / "voice-models"


def _model_files_present(model_id: str) -> bool:
    """Whether a download has already happened, without importing the engine.

    ``/status`` is polled by the interface and must stay cheap, so this looks at the
    filesystem rather than constructing a model to find out.
    """

    root = model_root()
    if not root.is_dir():
        return False
    needle = model_id.replace("/", "--").lower()
    for child in root.iterdir():
        if child.is_dir() and needle in child.name.lower():
            return any(child.rglob("model.bin")) or any(child.rglob("*.bin"))
    return False


def resolve_runtime() -> tuple[str, str, int]:
    """Device, compute type and thread count for this machine.

    ``float16`` is never chosen for the CPU. It is not a supported fast path there:
    CTranslate2 falls back to float32 and warns, so the result is *slower* than int8
    while looking like a decision that bought accuracy.

    Threads are capped at half the cores because Neo runs a local language model on the
    same machine and dictation must not starve token generation -- and because on
    macOS, spreading CTranslate2 across the efficiency cores makes it slower, not faster.
    """

    settings = get_settings()
    device = (settings.voice_device or "auto").strip().lower()
    compute = (settings.voice_compute_type or "auto").strip().lower()

    if device == "auto":
        device = "cpu"
        try:
            import ctranslate2

            if ctranslate2.get_cuda_device_count() > 0:
                device = "cuda"
        except Exception:
            device = "cpu"

    if compute == "auto":
        compute = "float16" if device == "cuda" else "int8"

    threads = int(settings.voice_cpu_threads or 0)
    if threads <= 0:
        threads = min(8, max(1, (os.cpu_count() or 4) // 2))
    return device, compute, threads


class FasterWhisperProvider:
    """A single Whisper model, loaded once and shared under a lock."""

    name = "faster_whisper"

    def __init__(self) -> None:
        self._model: Any = None
        self._key: tuple[str, str, str] | None = None
        self._lock = threading.Lock()

    # -- availability ------------------------------------------------------------

    def available(self) -> AvailabilityReport:
        settings = get_settings()
        if not settings.voice_enabled:
            return AvailabilityReport(
                available=False,
                reason="disabled",
                message="Voice input is switched off.",
                remedy="Turn it on in Settings, under Voice input.",
            )
        try:
            import faster_whisper  # noqa: F401
        except Exception:
            return AvailabilityReport(
                available=False,
                reason="dependency_missing",
                message="Speech recognition is not installed.",
                remedy='Run: pip install -e ".[voice]"',
            )
        model_id = settings.voice_model
        if not _model_files_present(model_id):
            return AvailabilityReport(
                available=False,
                reason="model_not_downloaded",
                message=f"The {model_id} speech model has not been downloaded yet.",
                remedy="Download it in Settings, under Voice input.",
            )
        return AvailabilityReport(available=True, reason="ready", message="Ready.")

    # -- model lifecycle ---------------------------------------------------------

    def _ensure(self) -> Any:
        """The loaded model, loading it if this is the first call.

        ``local_files_only=True`` on purpose: a transcription must never be the thing
        that silently starts a gigabyte download. Downloading is its own endpoint with
        its own progress and its own cancel.
        """

        settings = get_settings()
        device, compute, threads = resolve_runtime()
        key = (settings.voice_model, device, compute)

        with self._lock:
            if self._model is not None and self._key == key:
                return self._model
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                settings.voice_model,
                device=device,
                compute_type=compute,
                cpu_threads=threads,
                download_root=str(model_root()),
                local_files_only=True,
            )
            self._key = key
            return self._model

    def warm(self) -> None:
        """Load the model and run one tiny inference through it.

        The inference is the point. CTranslate2 allocates and specialises on first use,
        so without this the user's first dictation is several times slower than every
        one after it -- which reads as the feature being broken the first time, the one
        time a first impression is being formed.
        """

        model = self._ensure()
        silence = np.zeros(SAMPLE_RATE // 2, dtype=np.float32)
        segments, _ = model.transcribe(silence, language="en", beam_size=1, vad_filter=False)
        for _ in segments:
            break

    def unload(self) -> None:
        with self._lock:
            self._model = None
            self._key = None

    # -- transcription -----------------------------------------------------------

    def transcribe(
        self,
        audio: np.ndarray,
        options: TranscribeOptions,
        cancel: threading.Event | None = None,
    ) -> Transcript:
        model = self._ensure()
        # Every keyword here was checked against the installed faster-whisper (1.2.x)
        # rather than recalled. In particular the threshold is `log_prob_threshold`,
        # with the underscore -- OpenAI's own whisper spells the same idea
        # `logprob_threshold`, and passing that name raises TypeError on the first
        # transcription rather than being quietly ignored.
        segments, info = model.transcribe(
            audio,
            language=options.language or None,
            beam_size=options.beam_size,
            temperature=list(options.temperature),
            # Off, always. Conditioning on its own previous output is what makes
            # Whisper lock into a loop and repeat a phrase until the audio ends -- the
            # single most visible long-form failure, and in a composer it fills the box.
            condition_on_previous_text=options.condition_on_previous_text,
            # The model's own guards, set explicitly rather than left to drift with the
            # library's defaults. They decide whether a whole window is treated as
            # silence, which is the first line of defence against invented text.
            log_prob_threshold=options.log_prob_threshold,
            no_speech_threshold=options.no_speech_threshold,
            compression_ratio_threshold=options.compression_ratio_threshold,
            initial_prompt=options.initial_prompt or None,
            vad_filter=options.vad_filter,
            vad_parameters=options.vad_parameters if options.vad_filter else None,
            word_timestamps=False,
        )

        collected: list[Segment] = []
        # The generator is lazy: decoding happens as it is consumed, which is what lets
        # a cancelled dictation stop part-way instead of paying for the whole utterance.
        for segment in segments:
            if cancel is not None and cancel.is_set():
                break
            collected.append(
                Segment(
                    text=segment.text or "",
                    start=float(getattr(segment, "start", 0.0) or 0.0),
                    end=float(getattr(segment, "end", 0.0) or 0.0),
                    no_speech_prob=float(getattr(segment, "no_speech_prob", 0.0) or 0.0),
                    avg_logprob=float(getattr(segment, "avg_logprob", 0.0) or 0.0),
                    compression_ratio=float(getattr(segment, "compression_ratio", 1.0) or 1.0),
                )
            )

        return Transcript(
            segments=tuple(collected),
            language=str(getattr(info, "language", "") or ""),
            language_probability=float(getattr(info, "language_probability", 0.0) or 0.0),
            duration=float(getattr(info, "duration", 0.0) or 0.0),
            duration_after_vad=float(
                getattr(info, "duration_after_vad", getattr(info, "duration", 0.0)) or 0.0
            ),
        )
