"""Minimal, strict JSON-RPC framing for Language Server Protocol streams."""

from __future__ import annotations

import json


def encode(message: dict) -> bytes:
    """Return one JSON-RPC message using the LSP Content-Length framing."""
    body = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def decode(stream) -> dict:
    """Read one framed JSON-RPC message, rejecting malformed frames."""
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if not line:
            raise EOFError("LSP stream closed")
        if line in {b"\r\n", b"\n"}:
            break
        try:
            key, value = line.decode("ascii").split(":", 1)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("Malformed LSP header") from exc
        headers[key.lower().strip()] = value.strip()

    try:
        length = int(headers["content-length"])
    except (KeyError, ValueError) as exc:
        raise ValueError("LSP message is missing a valid Content-Length") from exc
    if length < 0 or length > 16 * 1024 * 1024:
        raise ValueError("LSP message Content-Length is out of bounds")

    body = stream.read(length)
    if len(body) != length:
        raise EOFError("LSP stream closed before a complete message was received")
    try:
        message = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Malformed LSP JSON message") from exc
    if not isinstance(message, dict):
        raise ValueError("LSP JSON-RPC payload must be an object")
    return message
