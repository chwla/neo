from __future__ import annotations

import hashlib


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def checksum_map(files: dict[str, bytes]) -> dict[str, str]:
    return {name: sha256_bytes(data) for name, data in sorted(files.items())}
