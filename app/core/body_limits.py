"""A ceiling on every request body, enforced before anything reads it.

Each upload route checked its size *after* Starlette had already spooled the whole
multipart body to a temporary file, and JSON routes read their whole body into
memory first -- including the public account-profile endpoints. Anyone who can
reach the port could fill the disk or the memory with one request.

This middleware sits outermost and bounds the body two ways: a declared
``Content-Length`` over the cap is refused before the application runs, and the
bytes actually received are counted as they arrive, so a chunked body without a
length stops at the cap too. Once a body has gone over, whatever the application
was about to answer -- FastAPI reports a broken body as a 400 -- is replaced by a
413 that says what the limit is.
"""

from __future__ import annotations

import json
import re

from app.core.config import get_base_settings

MIB = 1024 * 1024
#: Room for multipart boundaries and headers on top of the file itself.
_ENVELOPE = MIB
DEFAULT_LIMIT = 8 * MIB
BUNDLE_LIMIT = 50 * MIB + _ENVELOPE
#: 16 kHz, mono, 16-bit: the voice route's documented format.
_VOICE_BYTES_PER_SECOND = 16_000 * 2

_NOTE_TRANSITION_FILES = re.compile(r"^/api/notes/[^/]+/transitions/[^/]+/files$")


def limit_for(path: str) -> int:
    """The largest body ``path`` accepts, in bytes."""

    settings = get_base_settings()
    if path in {"/api/files/upload", "/api/notes/attachments"} or _NOTE_TRANSITION_FILES.match(
        path
    ):
        return settings.workspace_file_max_bytes + _ENVELOPE
    if path == "/api/gallery/items":
        return settings.gallery_image_max_bytes + _ENVELOPE
    if path == "/api/voice/transcribe":
        return (settings.voice_max_seconds + 1) * _VOICE_BYTES_PER_SECOND + 64 * 1024
    if path in {"/api/bundles/import", "/api/bundles/import/validate"}:
        return BUNDLE_LIMIT
    return DEFAULT_LIMIT


class _BodyTooLarge(Exception):
    pass


#: How much of an over-limit body is read and discarded before answering. A client
#: still sending when the connection closes sees a reset instead of the 413, so the
#: body is drained first -- up to a point, since the point of the limit is that an
#: arbitrary amount is never read.
DRAIN_CEILING = 64 * MIB


async def _drain(receive, already: int, ceiling: int) -> None:
    """Read and discard the rest of the body, so the client gets to see the answer."""

    total = already
    try:
        while total < ceiling:
            message = await receive()
            if message["type"] != "http.request":
                return
            total += len(message.get("body", b""))
            if not message.get("more_body", False):
                return
    except Exception:  # noqa: BLE001 - the client went away; nothing to drain
        return


async def _send_413(send, limit: int) -> None:
    body = json.dumps(
        {"detail": f"Request body is too large. The limit here is {limit // MIB} MB."}
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BodySizeLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in {"GET", "HEAD", "OPTIONS"}:
            await self.app(scope, receive, send)
            return
        limit = limit_for(scope["path"])
        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = limit + 1
                if declared > limit:
                    await _drain(receive, 0, min(declared, DRAIN_CEILING))
                    await _send_413(send, limit)
                    return

        received = 0
        exceeded = False
        replaced = False

        async def counted_receive():
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise _BodyTooLarge
            return message

        async def guarded_send(message):
            nonlocal replaced
            if exceeded:
                if not replaced:
                    replaced = True
                    await _drain(receive, received, DRAIN_CEILING)
                    await _send_413(send, limit)
                return
            await send(message)

        try:
            await self.app(scope, counted_receive, guarded_send)
        except BaseException:
            # The overflow may surface wrapped (an exception group from a middleware
            # task) or converted; the flag, not the exception's type, is what counts.
            if not exceeded:
                raise
        if exceeded and not replaced:
            replaced = True
            await _drain(receive, received, DRAIN_CEILING)
            await _send_413(send, limit)
