"""One anchored workspace write or delete, run in the tooling sandbox.

The target root is the process cwd chosen by Neo. Untrusted request bytes may name
only a relative path inside that root; they cannot supply a different root.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from app.services.isolation import safe_fs

MAX_DATA_BYTES = 4 * 1024 * 1024
MAX_REQUEST_BYTES = 6 * 1024 * 1024
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def apply_request(root: Path | int, request: dict[str, Any]) -> dict[str, Any]:
    """Apply only if the target still has the bytes the caller reviewed."""

    if not isinstance(request, dict):
        return {"ok": False, "reason": "invalid_request"}
    operation = request.get("operation")
    relative = request.get("relative")
    expect_exists = request.get("expect_exists")
    expected_hash = request.get("expected_sha256")
    root_dev = request.get("root_dev")
    root_ino = request.get("root_ino")
    permission_mode = request.get("permission_mode")
    if operation not in {"write", "delete"} or not isinstance(relative, str):
        return {"ok": False, "reason": "invalid_request"}
    if not isinstance(root_dev, int) or not isinstance(root_ino, int):
        return {"ok": False, "reason": "invalid_request"}
    if not isinstance(expect_exists, bool):
        return {"ok": False, "reason": "invalid_request"}
    if permission_mode is not None and permission_mode not in {0o600, 0o644, 0o755}:
        return {"ok": False, "reason": "invalid_request"}
    if operation == "delete" and permission_mode is not None:
        return {"ok": False, "reason": "invalid_request"}
    if expect_exists:
        if not isinstance(expected_hash, str) or not _DIGEST.fullmatch(expected_hash):
            return {"ok": False, "reason": "invalid_request"}
    elif expected_hash is not None:
        return {"ok": False, "reason": "invalid_request"}

    data: bytes | None = None
    if operation == "write":
        encoded = request.get("data_b64")
        if not isinstance(encoded, str) or len(encoded) > (MAX_DATA_BYTES * 4 // 3 + 8):
            return {"ok": False, "reason": "invalid_request"}
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            return {"ok": False, "reason": "invalid_request"}
        if len(data) > MAX_DATA_BYTES:
            return {"ok": False, "reason": "invalid_request"}
    elif request.get("data_b64") is not None:
        return {"ok": False, "reason": "invalid_request"}

    try:
        if safe_fs.root_identity(root) != (root_dev, root_ino):
            return {"ok": False, "reason": "changed"}
        if expect_exists:
            try:
                current = safe_fs.read_file(root, relative)
            except FileNotFoundError:
                return {"ok": False, "reason": "changed"}
            if hashlib.sha256(current).hexdigest() != expected_hash:
                return {"ok": False, "reason": "changed"}
        elif safe_fs.exists(root, relative):
            return {"ok": False, "reason": "changed"}

        if operation == "write":
            assert data is not None
            safe_fs.write_file(root, relative, data, permission_mode=permission_mode)
        else:
            safe_fs.delete(root, relative)
    except OSError:
        return {"ok": False, "reason": "unsafe_path_or_io"}
    return {"ok": True}


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        result = {"ok": False, "reason": "invalid_request"}
    else:
        try:
            request = json.loads(raw)
        except (ValueError, UnicodeError):
            request = None
        try:
            root_fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            result = {"ok": False, "reason": "unsafe_path_or_io"}
        else:
            try:
                result = apply_request(root_fd, request)
            finally:
                os.close(root_fd)
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
