"""HTTP surface for voice input.

Thin by design: parse, validate, hand off. Every decision lives in the service.

Nothing here does authentication. ProfileSessionMiddleware in app/main.py gates every
/api path centrally, which is what stops a new router shipping unprotected by omission
-- and it matters here, because these routes carry the user's recorded voice.

That same middleware is also why this is plain HTTP and not a WebSocket. It is a
Starlette ``BaseHTTPMiddleware``, so it only ever sees the ``http`` ASGI scope; a
WebSocket route would arrive unauthenticated *and* with no profile database bound. The
audio therefore travels as ordinary requests, and the progressive results come back as
newline-delimited JSON, which is the convention the chat and local-model routes already
use.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.services.voice import models as voice_models
from app.services.voice.audio import InvalidAudio, duration_seconds
from app.services.voice.service import VoiceService
from app.services.voice.worker import Busy

router = APIRouter(prefix="/voice", tags=["voice"])


def _service() -> VoiceService:
    return VoiceService()


@router.get("/status")
def status() -> dict:
    """Whether the microphone can be offered, and what to say when it cannot.

    Cheap on purpose: the composer polls this to decide how the dictation entry
    renders, so it checks the filesystem for the model rather than loading one.
    """

    return _service().status()


class InstallRequest(BaseModel):
    model_id: str = Field(min_length=1, max_length=64)


@router.get("/models")
def list_models() -> dict:
    """Every model Neo will install, and which of them are on disk."""

    return _service().models()


@router.post("/models/install")
def install_model(request: InstallRequest) -> StreamingResponse:
    """Download a model, reporting progress as it goes.

    The id is checked against the catalogue before anything touches the network.
    ``faster_whisper`` would happily resolve an arbitrary repository id or a
    filesystem path, so forwarding the browser's string would let any session with a
    cookie pull whatever it liked onto the machine and choose where it landed.

    Newline-delimited JSON for the same reason the local-model installer uses it: this
    is hundreds of megabytes, and a request that returns nothing for two minutes reads
    as a hung screen.
    """

    if not voice_models.is_allowed(request.model_id):
        raise HTTPException(status_code=404, detail="That is not a model Neo can install.")

    service = _service()

    def events() -> Iterator[bytes]:
        for event in service.install_model(request.model_id):
            yield (json.dumps(event) + "\n").encode()

    return StreamingResponse(events(), media_type="application/x-ndjson")


@router.post("/models/install/cancel")
def cancel_install(request: InstallRequest) -> dict:
    """Stop a download in progress.

    Aborting the browser's fetch does not stop it: the download runs in a server-side
    thread that knows nothing about that connection, so it needs its own signal.
    """

    if not voice_models.is_allowed(request.model_id):
        raise HTTPException(status_code=404, detail="That is not a model Neo can install.")
    return {"cancelled": voice_models.cancel(request.model_id)}


async def _read_audio(request: Request) -> bytes:
    """The request body as raw PCM, refusing anything longer than the cap.

    Read as raw bytes rather than through ``UploadFile``: that spools to a temporary
    file on disk past about a megabyte, and writing the user's voice to ``/tmp`` is
    exactly what "no audio is persisted" has to preclude.
    """

    # Raw PCM only. Anything else -- a form post, a JSON body, an audio container --
    # is a caller that has misunderstood the contract, and reading it as samples would
    # produce noise rather than an error.
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type and content_type != "application/octet-stream":
        raise HTTPException(
            status_code=415,
            detail="Audio must be sent as application/octet-stream (16 kHz mono 16-bit PCM).",
        )

    payload = await request.body()
    if not payload:
        raise HTTPException(status_code=400, detail="No audio was sent.")

    limit = get_settings().voice_max_seconds
    if duration_seconds(payload) > limit + 1:
        raise HTTPException(
            status_code=413,
            detail=f"That recording is longer than the {limit} second limit.",
        )
    return payload


@router.post("/transcribe")
async def transcribe(
    request: Request,
    language: str | None = Query(None, description="Pin a language, or omit for the default."),
    partial: bool = Query(False, description="Fast provisional decode rather than the final one."),
    spoken_punctuation: bool = Query(False),
) -> StreamingResponse:
    """Transcribe one whole recording, reporting progress as it goes.

    Newline-delimited JSON rather than a single response: a three-minute recording can
    take ten or twenty seconds on a laptop, and a composer that sits still that long
    reads as a hung screen. Matches the streaming convention used by the chat and
    local-model routes.
    """

    service = _service()
    report = service.availability()
    if not report.available:
        raise HTTPException(status_code=503, detail=report.message)

    payload = await _read_audio(request)

    def events() -> Iterator[bytes]:
        def emit(event: dict) -> bytes:
            return (json.dumps(event) + "\n").encode()

        yield emit(
            {
                "type": "status",
                "message": "Transcribing…",
                "seconds": round(duration_seconds(payload), 2),
            }
        )
        try:
            result = service.transcribe_queued(
                payload,
                language=language,
                partial=partial,
                spoken_punctuation=spoken_punctuation,
            )
        except Busy:
            yield emit({"type": "error", "code": "busy", "detail": "The speech engine is busy."})
            return
        except InvalidAudio as exc:
            yield emit({"type": "error", "code": "invalid_audio", "detail": str(exc)})
            return
        except Exception as exc:  # surfaced to the user rather than logged and swallowed
            yield emit({"type": "error", "code": "engine_error", "detail": str(exc)})
            return

        yield emit({"type": "final", **result})

    return StreamingResponse(events(), media_type="application/x-ndjson")
